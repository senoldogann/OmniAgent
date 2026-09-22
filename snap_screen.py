import asyncio
from tools import Toolbox

async def main():
    toolbox = Toolbox()
    toolbox.take_screenshot('full_screen_test.png')
    print("Screenshot taken")
    await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
