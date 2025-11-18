from nicegui import ui
import asyncio, csv, os, time, re, sys
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

# -----------------------------
# UoN stylesheet
# -----------------------------
ui.html("""
<style>
  .uon-header {
    background: #001E43;
    color: white;
    padding: 0.9rem 1.5rem;
    font-size: 1.4rem;
    font-weight: 600;
    font-family: system-ui, -apple-system, BlinkMacSystemFont,'Segoe UI',sans-serif;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
</style>
""", sanitize=False)


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


# --------------------------------------------------------
# EXTRACTION LOGIC
# --------------------------------------------------------
async def extract_feedback(report_url: str, output_path: str, log_box):
    global page

    start = time.perf_counter()

    await page.goto(report_url, wait_until="domcontentloaded")
    log_box.value += f"📄 Page title: {await page.title()}\n"

    hrefs = await page.eval_on_selector_all(
        QUESTION_LINK_SEL,
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )
    question_urls = [absolutize(h) for h in hrefs if h and 'textbox_marking' in h]

    if not question_urls:
        log_box.value += "⚠️ No questions found.\n"
        return None

    total_qs = len(question_urls)
    log_box.value += f"✅ Found {total_qs} questions.\n"

    await page.evaluate(f"""
        localStorage.setItem('totalQs', {total_qs});
        localStorage.setItem('currentQ', 0);
    """)

    # open CSV
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["question_number","student_id","student_label",
                         "mark","comment","student_answer"])

        for qi, qurl in enumerate(question_urls, 1):
            log_box.value += f"\n➡️ Processing Question {qi}/{total_qs}\n"

            await page.evaluate(f"""
                localStorage.setItem('currentQ', {qi});
                const total = {total_qs};
                const pct = Math.round(({qi} / total) * 100);
                let txt  = document.getElementById('__progress_txt__');
                let fill = document.getElementById('__progress_fill__');
                if (txt)  txt.textContent = 'Question {qi} of {total_qs} (' + pct + '%)';
                if (fill) fill.style.width = pct + '%';
            """)

            try:
                await page.goto(qurl, wait_until="domcontentloaded")
                await page.wait_for_selector(STUDENT_BLOCKS_SEL, timeout=10000)
            except Exception as e:
                log_box.value += f"⚠️ Failed to open {qurl}: {e}\n"
                continue

            blocks = page.locator(STUDENT_BLOCKS_SEL)
            count = await blocks.count()
            log_box.value += f"🧑‍🎓 Found {count} students\n"

            for si in range(count):
                b = blocks.nth(si)

                label = (await b.locator(HEADER_SEL).inner_text()).strip() \
                        if await b.locator(HEADER_SEL).count() else f"Student {si+1}"

                sid = (await b.locator(USERNAME_SEL).get_attribute("value")) or ""

                ans = (await b.locator(ANSWER_SEL).inner_text()).strip() \
                      if await b.locator(ANSWER_SEL).count() else ""

                mark = (await b.locator(MARK_SEL).inner_text()).strip() \
                       if await b.locator(MARK_SEL).count() else ""

                com = (await b.locator(COMMENT_SEL).input_value()).strip() \
                      if await b.locator(COMMENT_SEL).count() else ""

                writer.writerow([qi, sid, label, mark, com, ans])
                log_box.value += (
                    f"  ✅ {label} ({sid}) | mark={mark} "
                    f"| ans={len(ans)} chars | comm={len(com)} chars\n"
                )

            await page.goto(report_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.25)

    await page.evaluate("""
        let txt = document.getElementById('__progress_txt__');
        let fill = document.getElementById('__progress_fill__');
        if (txt) txt.textContent = '✅ Extraction Complete';
        if (fill) fill.style.width = '100%';
    """)

    elapsed = time.perf_counter() - start
    log_box.value += f"\n🎉 Done → {output_path}\n"

    ui.notify(f"✅ CSV saved: {os.path.basename(output_path)}", type="positive")
    ui.download(output_path, filename=os.path.basename(output_path))

    return elapsed


# --------------------------------------------------------
# PARSE SUMMARY
# --------------------------------------------------------
def parse_summary_from_log(log_text: str):
    q_matches = re.findall(r"^➡️ Processing Question", log_text, flags=re.MULTILINE)
    total_questions = len(q_matches)

    student_matches = re.findall(r"Found (\d+) students", log_text)
    total_students = max(map(int, student_matches)) if student_matches else 0

    row_matches = re.findall(r"^\s*✅", log_text, flags=re.MULTILINE)
    total_rows = len(row_matches)

    return total_questions, total_students, total_rows


# --------------------------------------------------------
# LOGIN + EXTRACTION
# --------------------------------------------------------
async def choose_and_extract(log_box, output_input, summary_labels):
    global pw, browser, ctx, page

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()

    # Inject exam button & progress bar
    await ctx.add_init_script("""
        (function(){
            function ensureProgressBox() {
                const total   = parseInt(localStorage.getItem('totalQs') || '0', 10);
                const current = parseInt(localStorage.getItem('currentQ') || '0', 10);
                const pct = (total > 0) ? Math.round((current/total)*100) : 0;

                let box = document.getElementById('__progress_box__');
                if (!box) {
                    box = document.createElement('div');
                    box.id = '__progress_box__';
                    Object.assign(box.style,{
                        position:'fixed',bottom:'30px',right:'30px',
                        width:'260px',height:'85px',
                        background:'#f57c00',color:'white',
                        padding:'1em',borderRadius:'10px',
                        fontFamily:'system-ui',zIndex:999999,
                        textAlign:'center',boxSizing:'border-box'
                    });
                    box.innerHTML=`
                        <div id="__progress_txt__" style="height:24px;line-height:24px;font-size:15px;">
                            ${pct>0?'Question '+current+' of '+total+' ('+pct+'%)':'0%'}
                        </div>
                        <div style="background:white;height:8px;border-radius:5px;overflow:hidden;margin-top:8px;">
                            <div id="__progress_fill__" style="height:8px;width:${pct}%;background:#4caf50;"></div>
                        </div>`;
                    document.body.appendChild(box);
                } else {
                    let txt=document.getElementById('__progress_txt__');
                    let fill=document.getElementById('__progress_fill__');
                    if (txt)  txt.textContent=(pct>0?'Question '+current+' of '+total+' ('+pct+'%)':'0%');
                    if (fill) fill.style.width=pct+'%';
                }
            }

            function render(){
                const extracting = (localStorage.getItem('__EXTRACT_MODE__')==='1');
                if (extracting){
                    let btn=document.getElementById('__exam_btn__'); if(btn) btn.remove();
                    ensureProgressBox();
                    return;
                }

                if(!document.getElementById('__exam_btn__')){
                    const btn=document.createElement('button');
                    btn.id='__exam_btn__';
                    btn.textContent='✅ This is my exam';
                    Object.assign(btn.style,{
                        position:'fixed',bottom:'30px',right:'30px',
                        background:'green',color:'white',
                        padding:'1em 1.5em',fontSize:'18px',
                        zIndex:999999,borderRadius:'10px',
                        border:'none',cursor:'pointer',
                        fontFamily:'system-ui'
                    });
                    btn.onclick=()=>{
                        localStorage.setItem('__EXTRACT_MODE__','1');
                        localStorage.setItem('totalQs','0');
                        localStorage.setItem('currentQ','0');
                        btn.remove();
                        if (window.examChosen) window.examChosen(window.location.href);
                        ensureProgressBox();
                    };
                    document.body.appendChild(btn);
                }
            }

            document.addEventListener('DOMContentLoaded',render);
            window.addEventListener('load',render);
            try{render();}catch(e){}
        })();
    """)

    try:
        exam_future = asyncio.Future()
        await page.expose_function("examChosen", lambda url: exam_future.set_result(url))

        log_box.value += "🔐 Opening ExamSys login page...\n"
        await page.goto(EXAMSYS_BASE)

        log_box.value += "🌐 Log in then click ‘This is my exam’.\n"
        chosen_url = await exam_future
        log_box.value += f"✅ Exam selected:\n{chosen_url}\n"

        await page.click("a:has-text('Reports')")
        await page.click("a:has-text('Primary Mark by Question')")
        await page.wait_for_load_state("domcontentloaded")

        report_url = page.url
        log_box.value += f"📄 Report page:\n{report_url}\n"

        log_box.value += "🚀 Starting extraction...\n"

        elapsed = await extract_feedback(report_url, output_input.value, log_box)

        # Parse summary
        log_text = log_box.value
        q, st, rows = parse_summary_from_log(log_text)

        summary_labels["questions"].text = str(q) if q else "–"
        summary_labels["students"].text  = str(st) if st else "–"
        summary_labels["rows"].text      = str(rows) if rows else "–"
        summary_labels["file"].text      = os.path.basename(output_input.value)
        summary_labels["duration"].text  = f"{elapsed:.1f}s" if elapsed else "–"

    finally:
        try: await ctx.close()
        except: pass
        try: await browser.close()
        except: pass
        try: await pw.stop()
        except: pass

        ui.notify("Extraction complete — browser closed", type="positive")
        log_box.value += "\n✅ Browser and session closed.\n"


# --------------------------------------------------------
# GUI LAYOUT
# --------------------------------------------------------
ui.html(
    '<div class="uon-header">🦜 ExamSys Short-Answer Extractor</div>',
    sanitize=False,
)

# Step 1 — filename
with ui.card().classes("w-full mt-4 p-4"):
    ui.label("Choose a name for your output CSV file").classes("text-lg font-semibold mb-2")
    output_in = ui.input(value=DEFAULT_OUTPUT).props('outlined dense clearable').classes("w-full")

summary_labels = {}

# Step 2 — login & extract
with ui.card().classes("w-full mt-4 p-4"):
    ui.label("Step 2: Log in and extract").classes("text-lg font-semibold mb-2")
    ui.label(
        "Click the button below, log in, navigate to the exam, then press ‘This is my exam’."
    ).classes("text-sm text-gray-700 mb-3")

    def autoscroll():
        ui.run_javascript(f"""
            const el = document.querySelector("#{log.id}");
            if (el) el.scrollTop = el.scrollHeight;
        """)

    async def start():
        log.value = "🌐 Opening ExamSys login page…\n"
        autoscroll()
        await choose_and_extract(log, output_in, summary_labels)

    ui.button("Login → Choose Exam → Extract", on_click=start) \
        .classes("mt-1 mb-3 bg-[#0095C8] text-white text-lg px-6 py-2 rounded")

# Summary card
with ui.card().classes("w-full mt-4 p-4"):
    ui.label("Extraction Summary").classes("text-lg font-semibold mb-3")

    def make_row(label, key):
        with ui.row().classes("justify-between w-full mb-1"):
            ui.label(label).classes("font-medium")
            summary_labels[key] = ui.label("–").classes("font-mono")

    make_row("Total questions:", "questions")
    make_row("Total students:", "students")
    make_row("Total CSV rows:", "rows")
    make_row("Output filename:", "file")
    make_row("Duration:", "duration")

# Log
with ui.expansion('Extraction Log', value=False).classes("mt-4 w-full"):
    log = ui.textarea().classes("w-full h-[34rem] text-base leading-relaxed").style("resize:none;overflow-y:scroll;")
    log.on('update:model-value', lambda _: autoscroll())


# --------------------------------------------------------
# SAFE SHUTDOWN BUTTON
# --------------------------------------------------------
async def shutdown_server():
    # Schedule notification on UI thread
    ui.timer(0.1, lambda: ui.notify("Shutting down…", type="info"), once=True)

    await asyncio.sleep(0.3)

    # Cleanly shut down NiceGUI
    await ui.get_app().shutdown()

    # Kill Python quietly
    os._exit(0)


ui.button(
    "Close App",
    on_click=lambda: asyncio.create_task(shutdown_server()),
).classes("mt-6 bg-red-600 text-white px-4 py-2 rounded")


# Credit watermark
ui.html("""
<div style="
    position:fixed;
    bottom:8px;right:12px;
    font-size:0.75rem;
    opacity:0.6;color:#4b5563;
    pointer-events:none;
    font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
    Developed by Dr Hannah Jackson: hannah.jackson@nottingham.ac.uk
</div>
""", sanitize=False)

ui.run(reload=False)