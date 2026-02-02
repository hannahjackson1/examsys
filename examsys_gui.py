######################################################
#                                                    #
#                                                    #
#  ExamSys SAQ Feedback Extractor (GUI)              #
#                                                    #
#  Author: Hannah Jackson                            #
#  Date: January 2026                                #
#                                                    #
#  This script provides a GUI for extracting SAQ     #
#  marks, feedback comments, and student responses   #
#  from ExamSys. The tool automates navigation of    #
#  the “Primary Mark by Question” report and exports #
#  structured feedback to CSV.                       #
#                                                    #
#                                                    #
######################################################


# -----------------------------------------------------------
# Import packages - these should be present in conda env
# -----------------------------------------------------------
from nicegui import ui, app
import asyncio, csv, os, time, re, sys, traceback, warnings
from pathlib import Path

# ------------------------------------------------------------
# Froce Playwright use its default local browser path (vs global cache)
# before importing. This is important as without it functionality
# breaks when packaging app into an executable for sharing.
# Will work locally without setting the default cache.
# ------------------------------------------------------------
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
from playwright.async_api import async_playwright


# ------------------------------------------------------------
# Configuration and state setup:
# Defining constants and placeholders that script relies on to run
# ------------------------------------------------------------

EXAMSYS_BASE = "https://examsys.nottingham.ac.uk" # This is the root URL for examsys, avoids hardocding URL elsewhere

# The following are the CSS selectors, which describe how to "find" elements on a webpage
# These look for specific HTML elements, and allow playwrite to interact with them/read them.
# Here, looking for links to questions, student answers, headers, answers, marks, comments.

QUESTION_LINK_SEL   = "a[href*='textbox_marking.php']" 
STUDENT_BLOCKS_SEL  = "div.student-answer-block.marked"
HEADER_SEL          = "p.theme"
ANSWER_SEL          = "div.student_ans"
MARK_SEL            = "select[id^='mark'] option:checked"
COMMENT_SEL         = "textarea[id^='comment']"
USERNAME_SEL        = "input[id^='username']"

pw = browser = ctx = page = None # Placeholders for playwright browser objects


# ------------------------------------------------------------
# Suppress the resource_tracker semaphore warnings on macOS
# (they're harmless but noisy, and fill up log files which
#  will be used to trace real errors)
# ------------------------------------------------------------

warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker: There appear to be .* leaked semaphore objects.*",
    category=UserWarning,
)

# ------------------------------------------------------------
# Create a writeable app directory for storing logs etc
# ~/Documents/ExamSysExtractor
# ------------------------------------------------------------
APP_DIR = Path.home() / "Documents" / "ExamSysExtractor" # This directory because is always writeable by user, and easy to find
LOG_DIR = APP_DIR / ".logs" # Creates a hidden folder in the app directory to store log files (user never needs to see these, but useful for debugging)
APP_DIR.mkdir(parents=True, exist_ok=True) # creates folders if they don't already exist
LOG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_OUTPUT = str(APP_DIR / "exam_feedback_by_question.csv") # ensures output goes (and is called) somewhere/thing sensible if defaults not changed

def new_log_file() -> str:
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    return str(LOG_DIR / f"examsys_log_{ts}.txt") # Generate a new log file every run, timestamped file name

CURRENT_LOG_FILE = None # will be set fresh on each extraction run, pointer for *this* extraction run

def append_log(text: str):
    """Append text safely to the current log file for this run."""
    global CURRENT_LOG_FILE
    if not CURRENT_LOG_FILE:
        CURRENT_LOG_FILE = new_log_file()
    try:
        with open(CURRENT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text) #appends to current log, does not overwrite. utf-8 allows non-ascii
    except Exception as e:
        print("Log write failed:", e, file=sys.stderr) # fails gracefully without crashing app


# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------

# absolutize takes relative links for e.g. ".reports/textbox_marking.php?id=123" and converts into a safe
# absolute URL which will not crash playwright (see base url definition above)

def absolutize(href: str, base: str = EXAMSYS_BASE) -> str:
    if not href:
        return ""
    href = href.strip() # strips whitespace
    if href.startswith("http"):
        return href # if link starts with http, already absolute so do nothing
    if href.startswith("/"):
        return f"{base.rstrip('/')}{href}" # handles site root paths
    if href.startswith("../"):
        return f"{base.rstrip('/')}/reports/{href.lstrip('../')}" # handles parent paths
    if href.startswith("reports/"):
        return f"{base.rstrip('/')}/{href}" # handles relative paths already starting with reports
    if href.startswith("textbox_marking"):
        return f"{base.rstrip('/')}/reports/{href}" # special case for handling empty file names
    return f"{base.rstrip('/')}/{href.lstrip('/')}"


# parse_mark_to_decimal forces 1/2 to be 0.5, to make .csv file play nicely with excel
def parse_mark_to_decimal(mark: str) -> str:
    """
    Convert marks like '1½' or '½' to '1.5' / '0.5' to avoid Excel weirdness.
    Leaves integers as-is. If parsing fails, returns original string.
    """
    if not mark:
        return "" # handles empty cells without falling over
    s = mark.strip()
    try:
        # handles the half-symbol format
        if "½" in s:
            if s == "½":
                return "0.5"
            # e.g. "1½", "2½"
            base = s.replace("½", "")
            base = base.strip()
            if base == "":
                return "0.5"
            # Some systems might show "1 ½"
            base = base.replace(" ", "")
            return str(float(base) + 0.5)

        # handle unformatted fractions like 1/2 or 3/2
        if re.fullmatch(r"\d+\s*/\s*\d+", s):
            num, den = re.split(r"\s*/\s*", s)
            den = float(den)
            if den != 0:
                return str(float(num) / den)

        # Plain numeric
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return s # leaves plain numeric strings unchanged

        return s
    except Exception:
        return s


# ------------------------------------------------------------
# Extraction logic, function definition
# ------------------------------------------------------------

# this bit is the core scraper functionality, once you reach the "primary mark by question"
# report page, it finds question links, visits each page, loops throug each student on page,
# pulls out info, writes it all to a .csv, and updates the on-page progress bar as it goes.

async def extract_feedback(report_url: str, output_path: str, log_box):
    global page

    append_log("\n=== Extraction Started ===\n") # record in the log that the extraction has started
    append_log(f"Report URL: {report_url}\n") # note the report URL (i.e. what exam it is!)

    start = time.perf_counter() # start a timer (useful for debugging if extraction takes ages for e.g.)

    try:
        await page.goto(report_url, wait_until="domcontentloaded") # open primary mark by q page in browser
        title = await page.title()
        msg = f"📄 Page title: {title}\n"
        log_box.value += msg
        append_log(msg) # append page title to log

        # find all links to individual question marking pages - these are usually relative links buried in the html
        hrefs = await page.eval_on_selector_all(
            QUESTION_LINK_SEL,
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)" # this bit is the javascript code to extract href attributes (web address)
        )

        # convert all the relative urls into absolute
        question_urls = [absolutize(h) for h in hrefs if h and 'textbox_marking' in h]

        # stop and throw an error if no questions are found
        if not question_urls:
            msg = "⚠️ No questions found.\n"
            log_box.value += msg
            append_log(msg)
            return None

        # count the number of questions to be processed
        total_qs = len(question_urls)
        msg = f"✅ Found {total_qs} questions.\n"
        log_box.value += msg
        append_log(msg)

        # store progress values so that the progress bar will function
        # without this, the bar resets to 0 every time a page is refreshed/loaded
        await page.evaluate(f"""
            localStorage.setItem('totalQs', {total_qs});
            localStorage.setItem('currentQ', 0);
        """)

        # check that the output directory exists
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # open the blank .csv file and write the column headers
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["question_number","student_id","student_label",
                             "mark","comment","student_answer"])

            # process each question, one at a time, looping through them all (starting at 1),
            # and updating on-screen progress message and log
            for qi, qurl in enumerate(question_urls, 1):
                msg = f"\n➡️ Processing Question {qi}/{total_qs}\n"
                log_box.value += msg
                append_log(msg)


                # update the progress bar display in browswer with % and fill
                await page.evaluate(f"""
                    localStorage.setItem('currentQ', {qi});
                    const total = {total_qs};
                    const pct = Math.round(({qi}/total)*100);
                    let txt=document.getElementById('__progress_txt__');
                    let fill=document.getElementById('__progress_fill__');
                    if(txt) txt.textContent = 'Question {qi} of {total_qs} ('+pct+'%)';
                    if(fill) fill.style.width = pct + '%';
                """)

                # open the marking page for current question in outer loop, wait for search
                try:
                    await page.goto(qurl, wait_until="domcontentloaded")
                    await page.wait_for_selector(STUDENT_BLOCKS_SEL, timeout=10000)

                # if search times out and page fails to load, log error and move on 
                except Exception as e:
                    err = f"⚠️ Failed to open {qurl}: {e}\n"
                    log_box.value += err
                    append_log(err)
                    continue

                # find all student blocks on the page
                # a student block is essentially the individual page containing marks, answers, drop downs etc  
                blocks = page.locator(STUDENT_BLOCKS_SEL)
                count = await blocks.count()
                msg = f"🧑‍🎓 Found {count} students\n"
                log_box.value += msg
                append_log(msg)

                # process each student answer
                for si in range(count):
                    b = blocks.nth(si)

                    # extract visible student label (else create a fallback by adding 1 to previous student)
                    label = (await b.locator(HEADER_SEL).inner_text()).strip() \
                            if await b.locator(HEADER_SEL).count() else f"Student {si+1}"

                    # extract student identifier (student number)
                    sid = (await b.locator(USERNAME_SEL).get_attribute("value")) or ""
                    ans = (await b.locator(ANSWER_SEL).inner_text()).strip() \
                          if await b.locator(ANSWER_SEL).count() else ""

                    # extract the selected mark, and convert 1/2 marks to decimals
                    mark_raw = (await b.locator(MARK_SEL).inner_text()).strip() \
                           if await b.locator(MARK_SEL).count() else ""
                    mark = parse_mark_to_decimal(mark_raw)

                    # extract the feedback comment
                    com = (await b.locator(COMMENT_SEL).input_value()).strip() \
                           if await b.locator(COMMENT_SEL).count() else ""

                    # write one row to the csv file for this student, and this question (i.e. student 3, question 1)
                    writer.writerow([qi, sid, label, mark, com, ans])

                    # log a short confirmation message, but not full answers
                    msg = (
                        f"  ✅ {label} ({sid}) | mark={mark} | "
                        f"ans={len(ans)} chars | comm={len(com)} chars\n"
                    )
                    log_box.value += msg
                    append_log(msg)

                # return to the main report page before navigating to the next question
                await page.goto(report_url, wait_until="domcontentloaded")
                await asyncio.sleep(0.25)

        # update progress bar to display that extraction is complete
        await page.evaluate("""
            let txt=document.getElementById('__progress_txt__');
            let fill=document.getElementById('__progress_fill__');
            if(txt) txt.textContent='✅ Extraction Complete';
            if(fill) fill.style.width='100%';
        """)

        # calculate total extraction time and note in log (as well as "done" message)
        elapsed = time.perf_counter() - start
        msg = f"\n🎉 Done → {out_path}\n"
        log_box.value += msg
        append_log(msg)
        append_log(f"Elapsed time: {elapsed:.2f}s\n")

        # notify that csv has been saved, and give location of save
        ui.notify(f"CSV saved: {out_path.name}", type="positive")
        ui.download(str(out_path), filename=out_path.name)

        return elapsed

    except Exception:
        # if anything funky happens, note full error in log
        append_log("EXCEPTION DURING EXTRACTION:\n")
        append_log(traceback.format_exc())
        raise


# ------------------------------------------------------------
# Parse summary from log function definition
# ------------------------------------------------------------

# read log and count how many questions were processed, how many students, how many
# rows written to csv. Only uses log, not the webpage or the csv file itself, thid

def parse_summary_from_log(log_text: str):
    q_matches = re.findall(r"^➡️ Processing Question", log_text, flags=re.MULTILINE)
    total_questions = len(q_matches)

    student_matches = re.findall(r"Found (\d+) students", log_text)
    total_students = max(map(int, student_matches)) if student_matches else 0

    row_matches = re.findall(r"^\s*✅ ", log_text, flags=re.MULTILINE)
    total_rows = len(row_matches)

    return total_questions, total_students, total_rows


# ------------------------------------------------------------
# Workflow functions: open browser, user chooses exam, perform extract logic
# ------------------------------------------------------------

async def choose_and_extract(log_box, output_input, summary_labels):
    # these are the shared playwright objects used across the app
    # playwright controller, browser, browser session, and active tab/page
    global pw, browser, ctx, page, CURRENT_LOG_FILE

    # New log file for this extraction run - start a new file so that each
    # extraction run has it's own timestamped log gile
    CURRENT_LOG_FILE = new_log_file()
    append_log(f"=== New Extraction Session: {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    # Start playwright (browser automation), launch a visible chromium browswer,
    # create a fresh context (browser session), and open a new tab
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context()
    page = await ctx.new_page()

    # inject exam button + progress bar - this only works with javascript which
    # runs on every page load in the browser session. This is for a floating
    # "this is my exam" button, and the orange progress bar seen during extraction

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
        # do nothing until user clicks "this is my exam" button in browser instance
        exam_future = asyncio.Future()
        # bridge function so that javascript from browser can send URL back into Python
        # essentially allows a javascript input (i.e. the browser button) to notify
        # the Python extraction logic, by passing back the URL for the correct exam
        await page.expose_function("examChosen", lambda url: exam_future.set_result(url))

        # tells the user whats happening (on screen and in log file)
        msg = "🔐 Opening ExamSys login page...\n"
        log_box.value += msg
        append_log(msg)

        # open examsys to allow the user to manually log in with university credentials
        # THESE CREDENTIALS ARE NOT STORED ANYWHERE
        await page.goto(EXAMSYS_BASE)

        # instructions
        msg = "🌐 Log in, navigate to exam, then click ‘This is my exam’.\n"
        log_box.value += msg
        append_log(msg)

        # wait until user clicks "This is my exam" button
        # the click triggers examChosen(...) and sets exam_future().
        chosen_url = await exam_future
        msg = f"✅ Exam selected:\n{chosen_url}\n"
        log_box.value += msg
        append_log(f"Exam chosen: {chosen_url}\n")

        # after exam is selected, click through examsys menus to reach the primary
        # mark by question report page
        await page.click("a:has-text('Reports')")
        await page.click("a:has-text('Primary Mark by Question')")
        await page.wait_for_load_state("domcontentloaded")

        # store the report URL (i.e. the page containing the links to each question)
        report_url = page.url
        msg = f"📄 Report page:\n{report_url}\n"
        log_box.value += msg
        append_log(f"Primary Mark by Question URL: {report_url}\n")

        msg = "🚀 Starting extraction...\n"
        log_box.value += msg
        append_log(msg)

        # run main extraction function, creating .csv file
        elapsed = await extract_feedback(report_url, output_input.value, log_box)

        # parse log summary for numbers
        tq, ts, tr = parse_summary_from_log(log_box.value)

        # update summary table
        summary_labels['questions'].text = str(tq)
        summary_labels['students'].text = str(ts)
        summary_labels['rows'].text = str(tr)
        summary_labels['file'].text = os.path.basename(output_input.value)
        summary_labels['path'].text = os.path.abspath(output_input.value)
        summary_labels['duration'].text = f"{elapsed:.1f}s"

        append_log("Summary updated\n")

    except Exception:
        # if something goes wrong, write the full error into log file for debugging
        append_log("EXCEPTION IN WORKFLOW:\n")
        append_log(traceback.format_exc())
        raise

    finally:
        # always close the browser, even if an error has occurred
        append_log("Closing Playwright...\n")

        # close context (browser session) and browser cleanly
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

        # stop playwright instance
        try:
            if pw:
                await pw.stop()   # Playwright uses stop(), not close()
        except Exception:
            append_log("Error while stopping Playwright:\n")
            append_log(traceback.format_exc())

        # notify user of completed extraction and browser close
        ui.notify("Extraction complete — browser closed", type="positive")
        log_box.value += "\n✅ Browser and session closed.\n"
        append_log("Browser and session closed.\n")


# ------------------------------------------------------------
# GUI Layout
# ------------------------------------------------------------

# this whole section builds the gui, defining the layout and appearance
# of the app window, including header, buttons, logs, summary etc.

# this is custom header and associated styling in the top banner
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
    top: 64px;
    left: 0;
    width: 100%;
    background: #f57c00;              /* Foxy orange */
    color: #FFF7ED;
    padding: 0.55rem 2rem;            /* slightly bigger + prominent */
    font-size: 1.15rem;               /* bigger subtitle */
    font-weight: 600;                 /* more prominent */
    letter-spacing: 0.2px;
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

# spacer so content does not slide under the fixed headers
ui.html("<div style='height:104px'></div>", sanitize=False)

# step 1
with ui.card().classes("w-full mt-2 p-4"):
    ui.label("Choose a name for your output CSV file").classes("text-lg font-semibold mb-2")
    output_in = ui.input(value=DEFAULT_OUTPUT).props('outlined dense clearable').classes("w-full text-base p-2")

summary_labels = {}

# step 2
with ui.card().classes("w-full mt-3 p-4"):
    ui.label("Step 2: Log in and extract").classes("text-lg font-semibold mb-2")
    ui.label(
        "Click the button below, then log in, choose your exam, and press ‘This is my exam’."
    ).classes("text-sm text-gray-700 mb-3")

    def autoscroll(): # autoscroll logic for log window
        ui.run_javascript(
            f"const el=document.querySelector('#{log.id}');"
            " if(el) el.scrollTop=el.scrollHeight;"
        )

    async def start(): # start button logic; clears log, scrolls to bottom, starts extraction workflow
        log.value = "🌐 Opening ExamSys login page…\n"
        autoscroll()
        await choose_and_extract(log, output_in, summary_labels)

    ui.button( # "go" button for app
        "Login → Choose Exam → Extract",
        on_click=start,
    ).classes("mt-1 mb-1 bg-[#0095C8] text-white text-lg px-6 py-2 rounded")


# summary panel
with ui.card().classes("w-full mt-3 p-3"):
    ui.label("Extraction Summary").classes("text-md font-semibold mb-2 text-[#7c2d12]")

    # summary grid layout definition
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
        .summary-label { opacity: 0.8; }
        .summary-value { text-align: right; }
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


# log window
with ui.expansion('Extraction Log', value=False).classes("mt-3 w-full"):
    log = ui.textarea().classes("w-full h-[34rem] text-base").style("resize:none;overflow-y:scroll;")
    log.on('update:model-value', lambda _: autoscroll())


# ------------------------------------------------------------
# Close App button with JS tab-close + extra cleanup
# ------------------------------------------------------------
async def shutdown_server():
    global pw, browser, ctx
    append_log("Shutdown requested by user via Close App button.\n")

    ui.run_javascript("""
        try {
            window.open('', '_self');
            window.close();
        } catch (e) {
            console.log('window.close() blocked:', e);
        }
    """)

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

    await asyncio.sleep(0.3)
    os._exit(0)

ui.button(
    "Close App",
    on_click=shutdown_server,
).classes("mt-5 bg-red-600 text-white px-4 py-2 rounded")


# Credit watermark
ui.html("""
<div style="
    position:fixed; bottom:8px; right:12px;
    font-size:0.75rem; opacity:0.6; color:#4b5563;
    pointer-events:none; font-family:system-ui;">
    Dr Hannah Jackson
</div>
""", sanitize=False)

ui.run(reload=False)