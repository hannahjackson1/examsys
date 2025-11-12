import asyncio
import csv
import os
from playwright.async_api import async_playwright

EXAMSYS_BASE = "https://examsys.nottingham.ac.uk"
REPORT_URL = f"{EXAMSYS_BASE}/reports/textbox_select_q.php?action=mark&phase=1&paperID=47081&startdate=20251013091500&enddate=20251031120000&repmodule=&repcourse=%&sortby=name&module=2544&folder=&percent=100&absent=0&studentsonly=1&ordering=asc"
OUTPUT_CSV = "exam_feedback_by_question.csv"

QUESTION_LINK_SEL = "a[href*='textbox_marking.php']"
STUDENT_BLOCKS_SEL = "div.student-answer-block.marked"
HEADER_SEL = "p.theme"
ANSWER_SEL = "div.student_ans"
MARK_SEL = "select[id^='mark'] option:checked"
COMMENT_SEL = "textarea[id^='comment']"
USERNAME_SEL = "input[id^='username']"  # hidden input with student ID


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=100)
        ctx = (
            await browser.new_context(storage_state="state.json")
            if os.path.exists("state.json")
            else await browser.new_context()
        )
        page = await ctx.new_page()

        # --- login if needed
        if not os.path.exists("state.json"):
            await page.goto(EXAMSYS_BASE)
            input("🔐 Log in via SSO, then press ENTER here… ")
            await ctx.storage_state(path="state.json")

        await page.goto(REPORT_URL, wait_until="domcontentloaded")
        print("Page title:", await page.title())

        links = await page.locator(QUESTION_LINK_SEL).all()
        print(f"Found {len(links)} questions.")
        if not links:
            await browser.close()
            return

        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["question_number", "student_id", "student_label", "student_answer", "mark", "comment"])

            # ---- iterate through each question
            for qi in range(len(links)):
                print(f"\nProcessing Question {qi+1}/{len(links)}")

                cur_links = await page.locator(QUESTION_LINK_SEL).all()
                if qi >= len(cur_links):
                    break

                await cur_links[qi].click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1500)

                # wait for all student blocks to load
                try:
                    await page.wait_for_selector(STUDENT_BLOCKS_SEL, timeout=10000)
                except:
                    print("⚠️ No student blocks found, skipping.")
                    continue

                blocks = page.locator(STUDENT_BLOCKS_SEL)
                count = await blocks.count()
                print(f"✅ Found {count} student blocks")

                for si in range(count):
                    block = blocks.nth(si)

                    # Extract student label, ID, answer, mark, and comment
                    try:
                        label = (await block.locator(HEADER_SEL).inner_text()).strip()
                    except:
                        label = f"Student {si+1}"

                    try:
                        student_id = (await block.locator(USERNAME_SEL).get_attribute("value")) or ""
                    except:
                        student_id = ""

                    try:
                        answer = (await block.locator(ANSWER_SEL).inner_text()).strip()
                    except:
                        answer = ""

                    try:
                        mark = (await block.locator(MARK_SEL).inner_text()).strip()
                    except:
                        mark = ""

                    try:
                        comment = (await block.locator(COMMENT_SEL).input_value()).strip()
                    except:
                        comment = ""

                    writer.writerow([qi+1, student_id, label, answer, mark, comment])
                    print(f"  ✅ {label} ({student_id}) | mark={mark} | ans={len(answer)} | comm={len(comment)}")

                print(f"Finished Question {qi+1} ({count} students).")

                # return to question list
                await page.goto(REPORT_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)

        await browser.close()
        print(f"\n✅ Done. Results saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    asyncio.run(main())