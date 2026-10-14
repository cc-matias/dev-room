#!/usr/bin/env python3
# Copyright (C) 2026 Matías Williams
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standalone local agent board. Not part of any one repository.

  python3 server.py [port]    # default 8791

Every agent has a name and an id. Posts store the id. Callers may send either
the name or the id. A feed post with no recipient is public.

The implementation lives in the devroom package. This file starts the process
and keeps the names other modules already import.
"""

import sys
import threading
import types

from devroom import state
from devroom.state import *
from devroom.store import *
from devroom.rules import *
from devroom.feed import *
from devroom.http import *


class _ServerModule(types.ModuleType):
    """server._BOARD and server._REV are the cache in devroom.state."""

    @property
    def _BOARD(self):
        return state._BOARD

    @_BOARD.setter
    def _BOARD(self, value):
        state._BOARD = value

    @property
    def _REV(self):
        return state._REV

    @_REV.setter
    def _REV(self, value):
        state._REV = value


sys.modules[__name__].__class__ = _ServerModule


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    with LOCK:
        data = load()
        if archive_old(data):
            save(data)
    threading.Thread(target=sweep_forever, daemon=True).start()
    server = BoardServer(("127.0.0.1", port), Handler)
    print(f"dev-room at http://localhost:{port}", flush=True)
    server.serve_forever()
