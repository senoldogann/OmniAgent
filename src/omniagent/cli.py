"""OmniAgent komut satırı giriş noktası."""
from __future__ import annotations

import argparse
import asyncio
from typing import Sequence

from omniagent.app.agent import run_agent
from omniagent.config import apply_stored_api_keys


def main(argv: Sequence[str] | None = None) -> None:
    """Tek bir doğal dil hedefini OmniAgent çalışma döngüsüne iletir."""
    parser = argparse.ArgumentParser(
        prog="omniagent",
        description="Yerel macOS otomasyon ajanı",
    )
    parser.add_argument("goal", nargs="+", help="OmniAgent'ın tamamlayacağı hedef")
    args = parser.parse_args(argv)

    apply_stored_api_keys()
    asyncio.run(run_agent(" ".join(args.goal).strip()))


if __name__ == "__main__":
    main()
