import asyncio
from tools import Toolbox

async def main():
    toolbox = Toolbox()
    toolbox.take_screenshot('ui_start.png')
    print("Screenshot taken successfully")
    await toolbox.close_browser()

if __name__ == "__main__":
    asyncio.run(main())
