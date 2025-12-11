from nicegui import ui, app
import asyncio, csv, os, time, re, sys, traceback
from playwright.async_api import async_playwright


EXAMSYS_BASE = "https://examsys.nottingham.ac.uk"
DEFAULT_OUTPUT = "exam_feedback_by_question.csv"

QUESTION_LINK_SEL   = "a[href*='textbox_marking.php']"
STUDENT_BLOCKS_SEL  = "div.student-answer-block.marked"
HEADER_SEL          = "p.theme"
ANSWER_SEL          = "div.student_ans"
MARK_SEL            = "select[id^='mark'] option:checked"
COMMENT_SEL         = "textarea[id^='comment']"
USERNAME_SEL        = "input[id^='username']"

pw = browser = ctx = page = None

# ------------------------------------------------------------
# Hidden .logs directory + timestamped log file
# ------------------------------------------------------------
LOG_DIR = ".logs"
os.makedirs(LOG_DIR, exist_ok=True)

def new_log_file() -> str:
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(LOG_DIR, f"examsys_log_{ts}.txt")

CURRENT_LOG_FILE = None  # will be set fresh on each extraction run

def append_log(text: str):
    """Append text safely to the current log file for this run."""
    global CURRENT_LOG_FILE
    if not CURRENT_LOG_FILE:
        CURRENT_LOG_FILE = new_log_file()
    try:
        with open(CURRENT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        print("Log write failed:", e, file=sys.stderr)


# ------------------------------------------------------------
# Header styling (base classes – some now unused but harmless)
# ------------------------------------------------------------
ui.html("""
<style>
  .header {
    background: #001E43;
    color: white;
    padding: 0.9rem 1.5rem;
    font-size: 1.4rem;
    font-weight: 600;
    font-family: system-ui, sans-serif;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
</style>
""", sanitize=False)

ui.html("""
<style>
  .subheader {
    background: #001E43;
    color: lightgrey;
    padding: 0.1rem 1.5rem;
    font-size: 1rem;
    font-weight: 200;
    font-family: system-ui, sans-serif;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
</style>
""", sanitize=False)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def absolutize(href: str, base: str = EXAMSYS_BASE) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"{base.rstrip('/')}{href}"
    if href.startswith("../"):
        return f"{base.rstrip('/')}/reports/{href.lstrip('../')}"
    if href.startswith("reports/"):
        return f"{base.rstrip('/')}/{href}"
    if href.startswith("textbox_marking"):
        return f"{base.rstrip('/')}/reports/{href}"
    return f"{base.rstrip('/')}/{href.lstrip('/')}"


def normalize_mark(mark: str) -> str:
    """
    Convert marks like '½', '1½', '2½' into decimal strings ('0.5', '1.5', '2.5')
    so Excel sees them as numeric values.

    If parsing fails, return the original mark.
    """
    if mark is None:
        return ""
    mark = mark.strip()
    if not mark:
        return ""

    # Simple half-mark handling
    if '½' in mark:
        # Just '½' on its own
        if mark == '½':
            return "0.5"
        # e.g. '1½', '2½', '3½'
        try:
            base = mark.replace('½', '').strip()
            base_val = int(base) if base else 0
            return f"{base_val + 0.5}"
        except ValueError:
            # If something unexpected, just leave it as-is
            return mark

    # Otherwise, leave unchanged (e.g. '0', '1', '2', '3', '4')
    return mark


# ------------------------------------------------------------
# Extraction logic
# ------------------------------------------------------------
async def extract_feedback(report_url: str, output_path: str, log_box):
    global page

    append_log("\n=== Extraction Started ===\n")
    append_log(f"Report URL: {report_url}\n")

    start = time.perf_counter()

    try:
        await page.goto(report_url, wait_until="domcontentloaded")
        title = await page.title()
        msg = f"📄 Page title: {title}\n"
        log_box.value += msg
        append_log(msg)

        hrefs = await page.eval_on_selector_all(
            QUESTION_LINK_SEL,
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
        )
        question_urls = [absolutize(h) for h in hrefs if h and 'textbox_marking' in h]

        if not question_urls:
            msg = "⚠️ No questions found.\n"
            log_box.value += msg
            append_log(msg)
            return None

        total_qs = len(question_urls)
        msg = f"✅ Found {total_qs} questions.\n"
        log_box.value += msg
        append_log(msg)

        await page.evaluate(f"""
            localStorage.setItem('totalQs', {total_qs});
            localStorage.setItem('currentQ', 0);
        """)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["question_number","student_id","student_label",
                             "mark","comment","student_answer"])

            for qi, qurl in enumerate(question_urls, 1):
                msg = f"\n➡️ Processing Question {qi}/{total_qs}\n"
                log_box.value += msg
                append_log(msg)

                await page.evaluate(f"""
                    localStorage.setItem('currentQ', {qi});
                    const total = {total_qs};
                    const pct = Math.round(({qi}/total)*100);
                    let txt=document.getElementById('__progress_txt__');
                    let fill=document.getElementById('__progress_fill__');
                    if(txt) txt.textContent = 'Question {qi} of {total_qs} ('+pct+'%)';
                    if(fill) fill.style.width = pct + '%';
                """)

                try:
                    await page.goto(qurl, wait_until="domcontentloaded")
                    await page.wait_for_selector(STUDENT_BLOCKS_SEL, timeout=10000)
                except Exception as e:
                    err = f"⚠️ Failed to open {qurl}: {e}\n"
                    log_box.value += err
                    append_log(err)
                    continue

                blocks = page.locator(STUDENT_BLOCKS_SEL)
                count = await blocks.count()
                msg = f"🧑‍🎓 Found {count} students\n"
                log_box.value += msg
                append_log(msg)

                for si in range(count):
                    b = blocks.nth(si)

                    label = (await b.locator(HEADER_SEL).inner_text()).strip() \
                            if await b.locator(HEADER_SEL).count() else f"Student {si+1}"

                    sid = (await b.locator(USERNAME_SEL).get_attribute("value")) or ""
                    ans = (await b.locator(ANSWER_SEL).inner_text()).strip() \
                          if await b.locator(ANSWER_SEL).count() else ""
                    mark_raw = (await b.locator(MARK_SEL).inner_text()).strip() \
                               if await b.locator(MARK_SEL).count() else ""
                    mark = normalize_mark(mark_raw)
                    com = (await b.locator(COMMENT_SEL).input_value()).strip() \
                           if await b.locator(COMMENT_SEL).count() else ""

                    writer.writerow([qi, sid, label, mark, com, ans])

                    msg = (
                        f"  ✅ {label} ({sid}) | mark={mark} | "
                        f"ans={len(ans)} chars | comm={len(com)} chars\n"
                    )
                    log_box.value += msg
                    append_log(msg)

                await page.goto(report_url, wait_until="domcontentloaded")
                await asyncio.sleep(0.25)

        await page.evaluate("""
            let txt=document.getElementById('__progress_txt__');
            let fill=document.getElementById('__progress_fill__');
            if(txt) txt.textContent='✅ Extraction Complete';
            if(fill) fill.style.width='100%';
        """)

        elapsed = time.perf_counter() - start
        msg = f"\n🎉 Done → {output_path}\n"
        log_box.value += msg
        append_log(msg)
        append_log(f"Elapsed time: {elapsed:.2f}s\n")

        ui.notify(f"CSV saved: {os.path.basename(output_path)}", type="positive")
        ui.download(output_path, filename=os.path.basename(output_path))

        return elapsed

    except Exception:
        append_log("EXCEPTION DURING EXTRACTION:\n")
        append_log(traceback.format_exc())
        raise


# ------------------------------------------------------------
# Parse summary from log
# ------------------------------------------------------------
def parse_summary_from_log(log_text: str):
    q_matches = re.findall(r"^➡️ Processing Question", log_text, flags=re.MULTILINE)
    total_questions = len(q_matches)

    student_matches = re.findall(r"Found (\d+) students", log_text)
    total_students = max(map(int, student_matches)) if student_matches else 0

    row_matches = re.findall(r"^\s*✅ ", log_text, flags=re.MULTILINE)
    total_rows = len(row_matches)

    return total_questions, total_students, total_rows


# ------------------------------------------------------------
# Workflow
# ------------------------------------------------------------
async def choose_and_extract(log_box, output_input, summary_labels):
    global pw, browser, ctx, page, CURRENT_LOG_FILE

    # New log file for this extraction run
    CURRENT_LOG_FILE = new_log_file()
    append_log(f"=== New Extraction Session: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()

    # Inject exam button + progress bar (working logic)
    await ctx.add_init_script("""
        (function(){
            function ensureProgressBox(){
                const total=parseInt(localStorage.getItem('totalQs')||'0');
                const cur=parseInt(localStorage.getItem('currentQ')||'0');
                const pct=total?Math.round((cur/total)*100):0;

                let box=document.getElementById('__progress_box__');
                if(!box){
                    box=document.createElement('div');
                    box.id='__progress_box__';
                    Object.assign(box.style,{
                        position:'fixed',bottom:'30px',right:'30px',
                        width:'260px',height:'85px',background:'#f57c00',
                        color:'white',padding:'1em',borderRadius:'10px',
                        fontFamily:'system-ui',zIndex:999999,
                        textAlign:'center',
                        boxShadow:'0 2px 6px rgba(0,0,0,0.3)',
                    });
                    box.innerHTML=`
                        <div id="__progress_txt__" style="font-size:15px;">
                            ${pct>0?'Question '+cur+' of '+total+' ('+pct+'%)':'0%'}
                        </div>
                        <div style="background:white;height:8px;border-radius:5px;margin-top:8px;">
                            <div id="__progress_fill__"
                                 style="height:8px;width:${pct}%;background:#4caf50;">
                            </div>
                        </div>`;
                    document.body.appendChild(box);
                } else {
                    let t=document.getElementById('__progress_txt__');
                    let f=document.getElementById('__progress_fill__');
                    if(t) t.textContent = pct>0 ? ('Question '+cur+' of '+total+' ('+pct+'%)') : '0%';
                    if(f) f.style.width = pct + '%';
                }
            }

            function render(){
                const extracting = localStorage.getItem('__EXTRACT_MODE__')==='1';
                if(extracting){
                    const b=document.getElementById('__exam_btn__');
                    if(b) b.remove();
                    ensureProgressBox();
                    return;
                }
                if(!document.getElementById('__exam_btn__')){
                    const b=document.createElement('button');
                    b.id='__exam_btn__';
                    b.textContent='✅ This is my exam';
                    Object.assign(b.style,{
                        position:'fixed',bottom:'30px',right:'30px',
                        background:'green',color:'white',
                        padding:'1em 1.5em',fontSize:'18px',zIndex:999999,
                        borderRadius:'10px',cursor:'pointer',
                        boxShadow:'0 2px 6px rgba(0,0,0,0.3)',
                    });
                    b.onclick=()=>{
                        localStorage.setItem('__EXTRACT_MODE__','1');
                        localStorage.setItem('totalQs','0');
                        localStorage.setItem('currentQ','0');
                        b.remove();
                        if(window.examChosen) window.examChosen(window.location.href);
                        ensureProgressBox();
                    };
                    document.body.appendChild(b);
                }
            }

            document.addEventListener('DOMContentLoaded',render);
            window.addEventListener('load',render);
        })();
    """)

    try:
        exam_future = asyncio.Future()
        await page.expose_function("examChosen", lambda url: exam_future.set_result(url))

        msg = "🔐 Opening ExamSys login page...\n"
        log_box.value += msg
        append_log(msg)

        await page.goto(EXAMSYS_BASE)

        msg = "🌐 Log in, navigate to exam, then click ‘This is my exam’.\n"
        log_box.value += msg
        append_log(msg)

        chosen_url = await exam_future
        msg = f"✅ Exam selected:\n{chosen_url}\n"
        log_box.value += msg
        append_log(f"Exam chosen: {chosen_url}\n")

        await page.click("a:has-text('Reports')")
        await page.click("a:has-text('Primary Mark by Question')")
        await page.wait_for_load_state("domcontentloaded")

        report_url = page.url
        msg = f"📄 Report page:\n{report_url}\n"
        log_box.value += msg
        append_log(f"Primary Mark by Question URL: {report_url}\n")

        msg = "🚀 Starting extraction...\n"
        log_box.value += msg
        append_log(msg)

        elapsed = await extract_feedback(report_url, output_input.value, log_box)

        # Parse log summary
        tq, ts, tr = parse_summary_from_log(log_box.value)

        summary_labels['questions'].text = str(tq)
        summary_labels['students'].text = str(ts)
        summary_labels['rows'].text = str(tr)
        summary_labels['file'].text = os.path.basename(output_input.value)
        summary_labels['path'].text = os.path.abspath(output_input.value)
        summary_labels['duration'].text = f"{elapsed:.1f}s"

        append_log("Summary updated\n")

    except Exception:
        append_log("EXCEPTION IN WORKFLOW:\n")
        append_log(traceback.format_exc())
        raise

    finally:
        append_log("Closing Playwright...\n")

        # Close context and browser cleanly
        try:
            if ctx:
                await ctx.close()
        except Exception:
            append_log("Error while closing context:\n")
            append_log(traceback.format_exc())

        try:
            if browser:
                await browser.close()
        except Exception:
            append_log("Error while closing browser:\n")
            append_log(traceback.format_exc())

        try:
            if pw:
                await pw.stop()   # Playwright uses stop(), not close()
        except Exception:
            append_log("Error while stopping Playwright:\n")
            append_log(traceback.format_exc())

        ui.notify("Extraction complete — browser closed", type="positive")
        log_box.value += "\n✅ Browser and session closed.\n"
        append_log("Browser and session closed.\n")


# ------------------------------------------------------------
# GUI Layout
# ------------------------------------------------------------
# FULL-WIDTH FIXED SPLIT HEADER:
# - main bar = academic navy
# - subheader bar = foxy orange accent, now more prominent
ui.html("""
<style>
  .fullwidth-header {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    background: #001E43;              /* Navy */
    color: white;
    padding: 1rem 2rem;
    font-size: 1.7rem;
    font-weight: 600;
    font-family: system-ui, sans-serif;
    z-index: 9999;
    box-sizing: border-box;
  }
  .fullwidth-subheader {
    position: fixed;
    top: 64px;                         /* sits just beneath header */
    left: 0;
    width: 100%;
    background: #f57c00;               /* Foxy orange accent */
    color: #FFF3E0;                    /* Slightly brighter soft text */
    padding: 0.6rem 2rem;              /* a bit taller */
    font-size: 1.2rem;                 /* bigger subtitle */
    font-weight: 600;                  /* bolder subtitle */
    font-family: system-ui, sans-serif;
    z-index: 9998;
    box-sizing: border-box;
  }
  body { margin: 0; }
</style>

<div class="fullwidth-header">
  🦊 FOXES: ExamSys Feedback Extractor
</div>

<div class="fullwidth-subheader">
  Feedback Output Xtractor: Examsys SAQ
</div>
""", sanitize=False)

# Spacer so content does not slide under the fixed headers
ui.html("<div style='height:45px'></div>", sanitize=False)

# Step 1
with ui.card().classes("w-full mt-4 p-4"):
    ui.label("Choose a name for your output CSV file").classes("text-lg font-semibold mb-2")
    output_in = ui.input(value=DEFAULT_OUTPUT).props('outlined dense clearable').classes("w-full text-base p-2")

summary_labels = {}

# Step 2
with ui.card().classes("w-full mt-4 p-4"):
    ui.label("Step 2: Log in and extract").classes("text-lg font-semibold mb-2")
    ui.label(
        "Click the button below, then log in, choose your exam, and press ‘This is my exam’."
    ).classes("text-sm text-gray-700 mb-3")

    def autoscroll():
        ui.run_javascript(
            f"const el=document.querySelector('#{log.id}');"
            " if(el) el.scrollTop=el.scrollHeight;"
        )

    async def start():
        # Reset UI log and kick off a new extraction run (which creates a new .logs file)
        log.value = "🌐 Opening ExamSys login page…\n"
        autoscroll()
        await choose_and_extract(log, output_in, summary_labels)

    ui.button(
        "Login → Choose Exam → Extract",
        on_click=start,
    ).classes("mt-1 mb-3 bg-[#0095C8] text-white text-lg px-6 py-2 rounded")


# ------------------------------------------------------------
# COMPACT TWO-COLUMN SUMMARY CARD (RIGHT-ALIGNED)
# ------------------------------------------------------------
with ui.card().classes("w-full mt-4 p-3"):
    ui.label("Extraction Summary").classes("text-md font-semibold mb-2 text-[#7c2d12]")

    ui.html("""
    <style>
        .summary-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px 18px;
            font-size: 0.85rem;
            color: #334155;
            align-items: center;
        }
        .summary-label {
            opacity: 0.8;
        }
        .summary-value {
            text-align: right;
        }
        .summary-footer {
            margin-top: 8px;
            font-size: 0.8rem;
            color: #475569;
            line-height: 1.25;
        }
    </style>
    """, sanitize=False)

    with ui.element("div").classes("summary-grid"):
        ui.label("Total questions:").classes("summary-label")
        summary_labels['questions'] = ui.label("–").classes("summary-value font-mono")

        ui.label("Students:").classes("summary-label")
        summary_labels['students'] = ui.label("–").classes("summary-value font-mono")

        ui.label("CSV rows:").classes("summary-label")
        summary_labels['rows'] = ui.label("–").classes("summary-value font-mono")

        ui.label("Duration:").classes("summary-label")
        summary_labels['duration'] = ui.label("–").classes("summary-value font-mono")

    with ui.element("div").classes("summary-footer"):
        with ui.row().classes("items-baseline gap-1"):
            ui.label("File:").classes("summary-label")
            summary_labels['file'] = ui.label("–").classes("font-mono text-xs truncate")
        with ui.row().classes("items-baseline gap-1 mt-1"):
            ui.label("Path:").classes("summary-label")
            summary_labels['path'] = ui.label("–").classes("font-mono text-[0.7rem] truncate")


# Log window
with ui.expansion('Extraction Log', value=False).classes("mt-4 w-full"):
    log = ui.textarea().classes("w-full h-[34rem] text-base").style("resize:none;overflow-y:scroll;")
    log.on('update:model-value', lambda _: autoscroll())


# ------------------------------------------------------------
# Close App button with JS tab-close + extra cleanup
# ------------------------------------------------------------
async def shutdown_server():
    global pw, browser, ctx
    append_log("Shutdown requested by user via Close App button.\n")

    # Ask browser tab/window to close (works in many launcher scenarios)
    ui.run_javascript("""
        try {
            window.open('', '_self');
            window.close();
        } catch (e) {
            console.log('window.close() blocked:', e);
        }
    """)

    # Try to cleanly close any remaining Playwright objects (if user closes mid-session)
    try:
        if ctx:
            await ctx.close()
    except Exception:
        append_log("Error while closing context in shutdown_server:\n")
        append_log(traceback.format_exc())

    try:
        if browser:
            await browser.close()
    except Exception:
        append_log("Error while closing browser in shutdown_server:\n")
        append_log(traceback.format_exc())

    try:
        if pw:
            await pw.stop()
    except Exception:
        append_log("Error while stopping Playwright in shutdown_server:\n")
        append_log(traceback.format_exc())

    # Small delay to let JS execute, then hard-exit
    await asyncio.sleep(0.3)
    os._exit(0)


ui.button(
    "Close App",
    on_click=shutdown_server,
).classes("mt-6 bg-red-600 text-white px-4 py-2 rounded")


# Credit watermark
ui.html("""
<div style="
    position:fixed; bottom:8px; right:12px;
    font-size:0.75rem; opacity:0.6; color:#4b5563;
    pointer-events:none; font-family:system-ui;">
    Hannah Jackson
</div>
""", sanitize=False)

ui.run(reload=False)