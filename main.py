"""
ELAW RPA - entry point.

Abre o HTML em janela nativa (Eel) e expoe a automacao Python (Playwright)
para a interface chamar.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import eel

from automation import browser as browser_mod
from automation import capa as capa_mod
from automation import documentos as documentos_mod
from automation import marcacao as marcacao_mod
from automation import pedidos as pedidos_mod
from automation import prazos as prazos_mod
from utils import reporter
from utils.spreadsheet import read_spreadsheet

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
LOG = logging.getLogger("elaw.main")


class _UILogHandler(logging.Handler):
    """Encaminha logs dos modulos elaw.* para o log da janela."""

    def emit(self, record):
        try:
            if record.name == "elaw.main":
                return
            if not record.name.startswith("elaw."):
                return
            short = record.name.replace("elaw.", "")
            msg = self.format(record)
            _js_call("notify_log", "  [" + short + "] " + msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Estado global
# ---------------------------------------------------------------------------
_state = {
    "rows": [],
    "spreadsheet_path": None,
    "running": False,
    "stop_flag": False,
    "headless": False,
    "results": [],
    "current_activity": None,
}

ACTIVITIES = {
    "capa": capa_mod,
    "prazos": prazos_mod,
    "pedidos": pedidos_mod,
    "documentos": documentos_mod,
    "marcacao": marcacao_mod,
}


# ---------------------------------------------------------------------------
# Helpers seguros para chamar JS
# ---------------------------------------------------------------------------
def _js_call(fn_name, *args):
    try:
        getattr(eel, fn_name)(*args)
    except Exception as e:
        LOG.debug("Falha ao chamar JS %s: %s", fn_name, e)


def notify_progress(idx, total, pasta_id, status, detail):
    _js_call("notify_progress", idx, total, pasta_id, status, detail)


def notify_log(line):
    _js_call("notify_log", line)


def notify_done(summary):
    _js_call("notify_done", summary)


# Conecta o handler de logging APOS _js_call estar definido
_ui_handler = _UILogHandler()
_ui_handler.setLevel(logging.INFO)
_ui_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger("elaw").addHandler(_ui_handler)


# ---------------------------------------------------------------------------
# API exposta ao JS
# ---------------------------------------------------------------------------
@eel.expose
def pick_spreadsheet():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Selecione a planilha de IDs",
            filetypes=[
                ("Planilhas", "*.xlsx *.xls *.csv *.tsv"),
                ("Todos", "*.*"),
            ],
        )
        root.destroy()
        if not path:
            return {"ok": False, "cancelled": True}

        rows = read_spreadsheet(path)
        ids = [r["A"] for r in rows]
        _state["rows"] = rows
        _state["spreadsheet_path"] = path

        return {
            "ok": True,
            "path": path,
            "filename": Path(path).name,
            "total": len(ids),
            "preview": ids[:5],
        }
    except Exception as e:
        LOG.error("Erro ao abrir planilha: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


@eel.expose
def start_run(payload):
    if _state["running"]:
        return {"ok": False, "error": "Ja existe uma execucao em andamento"}
    if not _state["rows"]:
        return {"ok": False, "error": "Nenhuma planilha carregada"}
    activity = payload.get("activity")
    if activity not in ACTIVITIES:
        return {"ok": False, "error": "Atividade invalida: " + str(activity)}

    _state["running"] = True
    _state["stop_flag"] = False
    _state["results"] = []
    _state["current_activity"] = activity

    thread = threading.Thread(target=_run_pipeline, args=(payload,), daemon=True)
    thread.start()
    return {"ok": True, "total": len(_state["rows"])}


@eel.expose
def stop_run():
    _state["stop_flag"] = True
    return {"ok": True}


@eel.expose
def save_report():
    try:
        out_dir = Path.home() / "Documents" / "ELAW_RPA_Relatorios"
        path = reporter.write_report(
            _state["results"],
            _state.get("current_activity") or "execucao",
            out_dir,
        )
        return {"ok": True, "path": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Mensagens amigaveis de erro
# ---------------------------------------------------------------------------
_FRIENDLY_BY_CONTEXT = {
    "abrir_pasta": "Nao consegui abrir a pasta",
    "capa": "Falha ao editar a Capa",
    "prazos": "Falha ao ajustar o Prazo",
    "pedidos": "Falha ao processar o Pedido",
    "documentos": "Falha ao processar Documentos",
    "marcacao": "Falha ao incluir Marcacao",
}


def _friendly_error(exc, contexto=""):
    """Converte uma excecao em mensagem amigavel para o relatorio."""
    raw = str(exc).split("\n")[0].strip()
    base = _FRIENDLY_BY_CONTEXT.get(contexto, "Erro")

    # RuntimeError (levantado por nos) - geralmente ja amigavel
    if isinstance(exc, RuntimeError):
        # 500 (e nao 200) para nao truncar a lista de campos obrigatorios
        # que a capa devolve quando o save e' bloqueado.
        return raw[:500] if raw else base

    # Erros do Playwright
    if "Timeout" in raw and "exceeded" in raw:
        if "Locator.click" in raw or "locator.click" in raw:
            return base + ": o sistema nao respondeu ao clique (timeout)"
        if "wait_for_selector" in raw:
            return base + ": elemento esperado nao apareceu (timeout)"
        if "Page.goto" in raw or "page.goto" in raw:
            return base + ": pagina demorou demais para carregar (timeout)"
        if "wait_for_url" in raw:
            return base + ": pagina nao mudou apos a acao (timeout)"
        return base + ": tempo esgotado aguardando o sistema"

    if "net::" in raw or "ERR_" in raw:
        return base + ": problema de conexao com o ELAW"

    if "is not visible" in raw or "not actionable" in raw:
        return base + ": o elemento esta na tela mas nao pode ser usado"

    if not raw:
        return base
    if len(raw) > 200:
        return base + ": " + raw[:197] + "..."
    return base + ": " + raw


# ---------------------------------------------------------------------------
# Pipeline de execucao
# ---------------------------------------------------------------------------
def _run_pipeline(payload):
    activity = payload["activity"]
    config = payload.get("config", {})
    creds_dict = payload.get("creds", {})
    activity_mod = ACTIVITIES[activity]

    rows = list(_state["rows"])
    total = len(rows)

    creds = browser_mod.Credentials(
        user=creds_dict.get("user", ""),
        password=creds_dict.get("password", ""),
    )

    notify_log("Iniciando atividade '" + activity + "' para " + str(total) + " ID(s)")
    started = time.time()

    try:
        with browser_mod.ElawSession(creds, headless=_state["headless"]) as sess:
            try:
                sess.login()
                notify_log("Login no ELAW efetuado com sucesso")
            except Exception as e:
                notify_log("ERRO no login: " + str(e))
                _finish(stopped=False, login_error=str(e))
                return

            for idx, row in enumerate(rows):
                if _state["stop_flag"]:
                    notify_log("Execucao interrompida pelo usuario")
                    break

                pasta_id = row.get("A", "").strip()
                if not pasta_id:
                    _record(idx, total, "", "skip", "ID vazio na linha")
                    continue

                notify_progress(idx, total, pasta_id, "running", "Processando...")
                notify_log("[%03d] ID %s - buscando pasta" % (idx + 1, pasta_id))

                try:
                    sess.open_pasta(pasta_id)
                except Exception as e:
                    msg = _friendly_error(e, contexto="abrir_pasta")
                    LOG.warning("Falha ao abrir pasta %s: %s", pasta_id, e)
                    _record(idx, total, pasta_id, "error", msg)
                    continue

                try:
                    if activity == "documentos":
                        detail = activity_mod.run(sess.page, config, row, pasta_id=pasta_id)
                    else:
                        detail = activity_mod.run(sess.page, config, row)
                    _record(idx, total, pasta_id, "ok", detail)
                except Exception as e:
                    msg = _friendly_error(e, contexto=activity)
                    LOG.warning("Falha em ID %s: %s", pasta_id, e)
                    _record(idx, total, pasta_id, "error", msg)

            elapsed = int(time.time() - started)
            _finish(stopped=_state["stop_flag"], elapsed=elapsed)

    except Exception as e:
        LOG.error("Erro fatal no pipeline: %s\n%s", e, traceback.format_exc())
        notify_log("ERRO FATAL: " + str(e))
        _finish(stopped=False, fatal_error=str(e))


def _record(idx, total, pasta_id, status, detail):
    entry = {"idx": idx + 1, "id": pasta_id, "status": status, "detail": detail}
    _state["results"].append(entry)
    notify_progress(idx, total, pasta_id, status, detail)
    notify_log("[%03d] ID %s - %s: %s" % (idx + 1, pasta_id, status.upper(), detail))


def _finish(stopped=False, elapsed=0, **extra):
    _state["running"] = False
    ok = sum(1 for r in _state["results"] if r["status"] == "ok")
    err = sum(1 for r in _state["results"] if r["status"] == "error")
    skip = sum(1 for r in _state["results"] if r["status"] == "skip")
    summary = {
        "total": len(_state["results"]),
        "ok": ok,
        "error": err,
        "skip": skip,
        "stopped": stopped,
        "elapsed": elapsed,
    }
    summary.update(extra)
    notify_done(summary)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Roda Playwright sem janela")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    _state["headless"] = args.headless

    web_dir = Path(__file__).resolve().parent / "web"
    eel.init(str(web_dir))

    LOG.info("Janela abrindo em http://localhost:%d/elaw_rpa.html", args.port)
    try:
        eel.start("elaw_rpa.html", port=args.port, size=(900, 800))
    except (SystemExit, KeyboardInterrupt):
        LOG.info("Janela fechada - encerrando.")


if __name__ == "__main__":
    main()
