import asyncio
from playwright.async_api import async_playwright

async def scan(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url)
        title = await page.title()
        await page.screenshot(path="outputs/screenshot.png")
        await browser.close()
        print(f"Title: {title}")
        print("Screenshot saved.")

asyncio.run(scan("https://todomvc.com/examples/react/dist"))
