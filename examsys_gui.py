from nicegui import ui
import asyncio, csv, os
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


# -------------------------------------------------------------------
# EXTRACTION LOGIC
# -------------------------------------------------------------------
async def extract_feedback(report_url: str, output_path: str, log_box):
    global page

    await page.goto(report_url, wait_until="domcontentloaded")
    log_box.value += f"📄 Page title: {await page.title()}\n"

    hrefs = await page.eval_on_selector_all(
        QUESTION_LINK_SEL,
        "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )
    question_urls = [absolutize(h) for h in hrefs if h and "textbox_marking" in h]

    if not question_urls:
        log_box.value += "⚠️ No question links found on the report page.\n"
        return

    total_qs = len(question_urls)
    log_box.value += f"✅ Found {total_qs} questions.\n"

    await page.evaluate(f"""
        localStorage.setItem('totalQs', {total_qs});
        localStorage.setItem('currentQ', 0);
    """)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["question_number", "student_id", "student_label", "mark", "comment", "student_answer"])

        for qi, qurl in enumerate(question_urls, 1):

            log_box.value += f"\n➡️ Processing Question {qi}/{total_qs}\n"

            await page.evaluate(f"""
                localStorage.setItem('currentQ', {qi});
                const total = {total_qs};
                const current = {qi};
                const pct = Math.round((current / total) * 100);

                let fill = document.getElementById('__progress_fill__');
                let txt  = document.getElementById('__progress_txt__');
                if (fill) fill.style.width = pct + '%';
                if (txt)  txt.textContent = 'Question ' + current + ' of ' + total + ' (' + pct + '%)';
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

                label = (await b.locator(HEADER_SEL).inner_text()).strip() if await b.locator(HEADER_SEL).count() else f"Student {si+1}"
                sid = (await b.locator(USERNAME_SEL).get_attribute("value")) or ""
                ans = (await b.locator(ANSWER_SEL).inner_text()).strip() if await b.locator(ANSWER_SEL).count() else ""
                mark = (await b.locator(MARK_SEL).inner_text()).strip() if await b.locator(MARK_SEL).count() else ""
                com = (await b.locator(COMMENT_SEL).input_value()).strip() if await b.locator(COMMENT_SEL).count() else ""

                writer.writerow([qi, sid, label, mark, com, ans])
                log_box.value += f"  ✅ {label} ({sid}) | mark={mark} | ans={len(ans)} | comm={len(com)}\n"

            await page.goto(report_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.25)

    await page.evaluate("""
        const fill = document.getElementById('__progress_fill__');
        const txt  = document.getElementById('__progress_txt__');
        if (fill) fill.style.width = '100%';
        if (txt)  txt.textContent = '✅ Extraction Complete';
    """)

    log_box.value += f"\n🎉 Done → {output_path}\n"
    ui.notify(f"CSV saved: {os.path.basename(output_path)}", type="positive")
    ui.download(output_path, label="📥 Download CSV").classes("bg-green-600 text-white mt-2 px-3 py-1 rounded")


# -------------------------------------------------------------------
# LOGIN FLOW + PROGRESS BAR INJECTION
# -------------------------------------------------------------------
async def choose_and_extract(log_box, output_input):
    global pw, browser, ctx, page

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()

    await ctx.add_init_script("""
        (function () {
            function showProgress() {
                const total = parseInt(localStorage.getItem('totalQs') || '0');
                const curr  = parseInt(localStorage.getItem('currentQ') || '0');
                const pct   = total > 0 ? Math.round((curr / total) * 100) : 0;

                let box = document.getElementById('__progress_box__');
                if (!box) {
                    box = document.createElement('div');
                    box.id = '__progress_box__';
                    Object.assign(box.style, {
                        position:'fixed', bottom:'30px', right:'30px',
                        width:'260px', height:'85px',
                        background:'#f57c00', color:'white',
                        padding:'1em', borderRadius:'10px',
                        zIndex:999999, fontFamily:'system-ui',
                        boxShadow:'0 2px 6px rgba(0,0,0,0.3)'
                    });
                    box.innerHTML = `
                        <div id="__progress_txt__"
                             style="font-size:15px; margin-bottom:8px;">
                             ${pct}%
                        </div>
                        <div style="background:white;width:100%;height:8px;border-radius:6px;overflow:hidden;">
                            <div id="__progress_fill__"
                                 style="height:8px;width:${pct}%;background:#4caf50;transition:width 0.4s;">
                            </div>
                        </div>`;
                    document.body.appendChild(box);
                } else {
                    document.getElementById('__progress_fill__').style.width = pct + '%';
                    document.getElementById('__progress_txt__').textContent =
                        (total > 0 ? `Question ${curr} of ${total} (${pct}%)` : '0%');
                }
            }

            function render() {
                const extracting = (localStorage.getItem('__EXTRACT_MODE__') === '1');
                if (extracting) {
                    const btn = document.getElementById('__exam_btn__');
                    if (btn) btn.remove();
                    showProgress();
                    return;
                }

                if (!document.getElementById('__exam_btn__')) {
                    const btn = document.createElement('button');
                    btn.id = '__exam_btn__';
                    btn.textContent = '✅ This is my exam';
                    Object.assign(btn.style, {
                        position:'fixed', bottom:'30px', right:'30px',
                        background:'green', color:'white',
                        padding:'1em 1.5em', fontSize:'18px',
                        borderRadius:'10px', border:'none',
                        zIndex:999999, cursor:'pointer'
                    });
                    btn.onclick = () => {
                        localStorage.setItem('__EXTRACT_MODE__', '1');
                        localStorage.setItem('totalQs', 0);
                        localStorage.setItem('currentQ', 0);
                        btn.remove();
                        if (window.examChosen) window.examChosen(window.location.href);
                        showProgress();
                    };
                    document.body.appendChild(btn);
                }
            }

            document.addEventListener('DOMContentLoaded', render);
            window.addEventListener('load', render);
            render();
        })();
    """)

    # ---- Wait for "This is my exam"
    exam_future = asyncio.Future()
    await page.expose_function("examChosen", lambda url: exam_future.set_result(url))

    log_box.value += "🔐 Opening ExamSys login page...\n"
    await page.goto(EXAMSYS_BASE)

    log_box.value += "🌐 Log in → navigate to your exam → click 'This is my exam'.\n"
    chosen_url = await exam_future
    log_box.value += f"✅ Exam selected: {chosen_url}\n"

    # ---- Navigate to reports
    await page.wait_for_selector("a:has-text('Reports')", timeout=10000)
    await page.click("a:has-text('Reports')")
    await page.wait_for_selector("a:has-text('Primary Mark by Question')", timeout=10000)
    await page.click("a:has-text('Primary Mark by Question')")
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(1)

    report_url = page.url
    log_box.value += f"📄 Reached report page:\n{report_url}\n"
    log_box.value += "🚀 Starting extraction…\n"

    await extract_feedback(report_url, output_input.value, log_box)

    # ---- Close Chromium window after extraction
    await browser.close()
    await pw.stop()
    log_box.value += "\n🛑 Browser closed.\n"
    ui.notify("Extraction complete — browser window closed", type="positive")


# -------------------------------------------------------------------
# GUI LAYOUT (UoN themed, professional)
# -------------------------------------------------------------------

# --- UoN Header with Parrot ---
ui.html("""
<div style="
    background:#00563F;
    color:white;
    padding:1.4rem;
    border-radius:10px;
    font-family:system-ui;
    margin-bottom:1rem;
">
    <div style="font-size:2rem; font-weight:600;">
        🦜 ExamSys Short-Answer Extractor
    </div>
    <div style="opacity:0.85; margin-top:6px;">
        University of Nottingham – Automated Feedback Retrieval Tool
    </div>
</div>
""", sanitize=False)

# --- Main layout (two columns) ---
with ui.row().classes("w-full gap-6"):

    # LEFT COLUMN — controls
    with ui.column().classes("w-1/3"):

        ui.label("Choose a name for your output CSV file").classes("text-lg font-semibold")
        output_in = ui.input(value=DEFAULT_OUTPUT).props('outlined dense clearable').classes("w-full mb-6")

        ui.button("Login → Choose Exam → Extract",
                  on_click=lambda: asyncio.create_task(choose_and_extract(log, output_in))
                 ).classes("bg-blue-600 text-white text-lg px-6 py-3 rounded shadow-md")

    # RIGHT COLUMN — extraction log
    with ui.column().classes("w-2/3"):

        ui.label("Extraction Log").classes("font-bold text-lg")
        log = ui.textarea().classes(
            "w-full h-[30rem] text-base leading-relaxed p-3 bg-white border border-gray-300 rounded"
        ).style("resize:none; overflow-y:scroll;")


# --- Footer credit ---
ui.html("""
<div style="margin-top:20px; text-align:right; opacity:0.7; font-size:0.9rem;">
    Developed by <strong>Hannah Jackson</strong> © 2025
</div>
""", sanitize=False)

ui.run(reload=False)