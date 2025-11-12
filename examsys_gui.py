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


# ---------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------
async def extract_feedback(report_url: str, output_path: str, log_box):
    global page

    await page.goto(report_url, wait_until="domcontentloaded")
    log_box.value += f"📄 Page title: {await page.title()}\n"

    hrefs = await page.eval_on_selector_all(
        QUESTION_LINK_SEL, "els => els.map(e => e.getAttribute('href')).filter(Boolean)"
    )
    question_urls = [absolutize(h) for h in hrefs if h and "textbox_marking" in h]

    if not question_urls:
        log_box.value += "⚠️ No question links found on the report page.\n"
        return

    total_qs = len(question_urls)
    log_box.value += f"✅ Found {total_qs} questions.\n"

    # Store total questions persistently
    await page.evaluate(f"localStorage.setItem('totalQs', {total_qs}); localStorage.setItem('currentQ', 0);")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["question_number", "student_id", "student_label", "mark", "comment", "student_answer"])

        for qi, qurl in enumerate(question_urls, 1):
            log_box.value += f"\n➡️ Processing Question {qi}/{total_qs}\n"
            try:
                await page.goto(qurl, wait_until="domcontentloaded")
                await page.wait_for_selector(STUDENT_BLOCKS_SEL, timeout=10000)
            except Exception as e:
                log_box.value += f"⚠️ Failed to open {qurl}: {e}\n"
                continue

            # --- update progress (and persist it) ---
            await page.evaluate(f"""
                localStorage.setItem('currentQ', {qi});
                const total = parseInt(localStorage.getItem('totalQs')) || {total_qs};
                const current = parseInt(localStorage.getItem('currentQ')) || {qi};
                const frac = current / total;

                // Ensure the progress box exists
                let box = document.getElementById('__progress_box__');
                if (!box) {{
                    box = document.createElement('div');
                    box.id = '__progress_box__';
                    Object.assign(box.style, {{
                        position: 'fixed', bottom: '30px', right: '30px', width: '260px',
                        background: '#f57c00', color: 'white', padding: '1em',
                        borderRadius: '10px', fontFamily: 'system-ui', zIndex: 999999,
                        boxShadow: '0 2px 6px rgba(0,0,0,0.3)', textAlign: 'center'
                    }});
                    box.innerHTML = `
                        <div id="__progress_txt__">0%</div>
                        <div style="background:white;height:8px;border-radius:5px;overflow:hidden;margin-top:8px;">
                            <div id="__progress_fill__" style="
                                height:8px;
                                width:0%;
                                background:#4caf50;
                                transition:width 0.4s ease;
                            "></div>
                        </div>`;
                    document.body.appendChild(box);
                }}

                const fill = document.getElementById('__progress_fill__');
                const txt = document.getElementById('__progress_txt__');
                if (fill) fill.style.width = (frac * 100) + '%';
                if (txt) txt.textContent = 'Question ' + current + ' of ' + total + ' (' + Math.round(frac * 100) + '%)';
            """)

            blocks = page.locator(STUDENT_BLOCKS_SEL)
            count = await blocks.count()
            log_box.value += f"🧑‍🎓 Found {count} students\n"

            for si in range(count):
                b = blocks.nth(si)
                label = await b.locator(HEADER_SEL).inner_text() if await b.locator(HEADER_SEL).count() else f"Student {si+1}"
                sid = await b.locator(USERNAME_SEL).get_attribute("value") or ""
                ans = await b.locator(ANSWER_SEL).inner_text() if await b.locator(ANSWER_SEL).count() else ""
                mark = await b.locator(MARK_SEL).inner_text() if await b.locator(MARK_SEL).count() else ""
                com = await b.locator(COMMENT_SEL).input_value() if await b.locator(COMMENT_SEL).count() else ""

                writer.writerow([qi, sid, label.strip(), mark.strip(), com.strip(), ans.strip()])
                log_box.value += f"  ✅ {label} ({sid}) | mark={mark.strip()} | ans={len(ans)} | comm={len(com)}\n"

            await page.goto(report_url, wait_until="domcontentloaded")
            await asyncio.sleep(0.25)

    # --- mark complete visually ---
    await page.evaluate("""
        const fill = document.getElementById('__progress_fill__');
        const txt = document.getElementById('__progress_txt__');
        if (fill) {
            fill.style.width = '100%';
            fill.style.background = '#4caf50';
        }
        if (txt) txt.textContent = '✅ Extraction Complete';
    """)

    log_box.value += f"\n🎉 Done → {output_path}\n"
    ui.notify(f"✅ CSV saved: {os.path.basename(output_path)}", type="positive")
    ui.download(output_path, label="⬇️ Download CSV",
                filename=os.path.basename(output_path)).classes("bg-green-600 text-white mt-2 px-3 py-1 rounded")


# ---------------------------------------------------------------------
# Login + navigation flow
# ---------------------------------------------------------------------
async def choose_and_extract(log_box, output_input):
    global pw, browser, ctx, page
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()

    # persistent UI injection (survives reloads)
    await ctx.add_init_script("""
        (function(){
            function render(){
                const extracting = (localStorage.getItem('__EXTRACT_MODE__') === '1');
                const total = parseInt(localStorage.getItem('totalQs')) || 0;
                const current = parseInt(localStorage.getItem('currentQ')) || 0;
                const frac = total ? current / total : 0;
                if(extracting){
                    const oldBtn=document.getElementById('__exam_btn__');
                    if(oldBtn) oldBtn.remove();
                    let box=document.getElementById('__progress_box__');
                    if(!box){
                        box=document.createElement('div');
                        box.id='__progress_box__';
                        Object.assign(box.style,{
                            position:'fixed',bottom:'30px',right:'30px',width:'260px',
                            background:'#f57c00',color:'white',padding:'1em',
                            borderRadius:'10px',fontFamily:'system-ui',zIndex:999999,
                            boxShadow:'0 2px 6px rgba(0,0,0,0.3)',textAlign:'center'
                        });
                        box.innerHTML=`
                            <div id="__progress_txt__">0%</div>
                            <div style="background:white;height:8px;border-radius:5px;overflow:hidden;margin-top:8px;">
                                <div id="__progress_fill__" style="
                                    height:8px;
                                    width:${frac*100}%;
                                    background:#4caf50;
                                    transition:width 0.4s ease;
                                "></div>
                            </div>`;
                        document.body.appendChild(box);
                    } else {
                        const fill=document.getElementById('__progress_fill__');
                        if(fill) fill.style.width=(frac*100)+'%';
                        const txt=document.getElementById('__progress_txt__');
                        if(txt && total>0) txt.textContent='Question '+current+' of '+total+' ('+Math.round(frac*100)+'%)';
                    }
                    return;
                }
                if(!document.getElementById('__exam_btn__')){
                    const btn=document.createElement('button');
                    btn.id='__exam_btn__';
                    btn.textContent='✅ This is my exam';
                    Object.assign(btn.style,{
                        position:'fixed',bottom:'30px',right:'30px',
                        background:'green',color:'white',padding:'1em 1.5em',
                        fontSize:'18px',zIndex:999999,borderRadius:'10px',border:'none',
                        cursor:'pointer',fontFamily:'system-ui',
                        boxShadow:'0 2px 6px rgba(0,0,0,0.3)'
                    });
                    btn.onclick=()=>{
                        localStorage.setItem('__EXTRACT_MODE__','1');
                        btn.remove();
                        localStorage.setItem('totalQs', 0);
                        localStorage.setItem('currentQ', 0);
                        render();
                        if(window.examChosen) window.examChosen(window.location.href);
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

        log_box.value += "🌐 Log in via SSO, navigate to your exam, then click ✅ This is my exam.\n"
        chosen_url = await exam_future
        log_box.value += f"✅ Exam selected: {chosen_url}\n"

        # Navigate to Reports → Primary Mark by Question
        await page.wait_for_selector("a:has-text('Reports')", timeout=10000)
        await page.click("a:has-text('Reports')")
        await page.wait_for_selector("a:has-text('Primary Mark by Question')", timeout=10000)
        await page.click("a:has-text('Primary Mark by Question')")
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(1)
        report_url = page.url
        log_box.value += f"✅ Arrived at report page:\n{report_url}\n"

        log_box.value += "🚀 Starting extraction...\n"
        await extract_feedback(report_url, output_input.value, log_box)

    finally:
        try:
            if ctx: await ctx.close()
            if browser: await browser.close()
            if pw: await pw.stop()
        except Exception:
            pass
        ui.notify("✅ Extraction complete — browser closed", type="positive")
        log_box.value += "\n✅ Browser and session closed.\n"


# ---------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------
ui.label("🦜 ExamSys Feedback Extractor").classes("text-2xl font-bold mt-4")
output_in = ui.input("Output CSV filename", value=DEFAULT_OUTPUT).classes("w-full")

with ui.card().classes("w-full mt-4"):
    ui.label("Extraction Log").classes("font-bold")
    log = ui.textarea().classes("w-full h-96").style("resize:none;")

def start():
    log.value = "🌐 Opening ExamSys login page…\n"
    asyncio.create_task(choose_and_extract(log, output_in))

ui.button("Login → Choose Exam → Extract",
          on_click=start).classes("mt-4 bg-blue-600 text-white")

ui.run(reload=False)