"""
ELAW RPA - Web Server
FastAPI + WebSockets substitui Eel para deploy no Render.
main.py original continua funcional para uso local/desktop.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from automation import browser as browser_mod
from automation import capa as capa_mod
from automation import documentos as documentos_mod
from automation import marcacao as marcacao_mod
from automation import pedidos as pedidos_mod
from automation import prazos as prazos_mod
from utils import reporter
from utils.spreadsheet import read_spreadsheet

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
LOG = logging.getLogger("elaw.server")

app = FastAPI(title="ELAW RPA")

_web_dir = Path(__file__).parent / "web"

ACTIVITIES = {
    "capa": capa_mod,
    "prazos": prazos_mod,
    "pedidos": pedidos_mod,
    "documentos": documentos_mod,
    "marcacao": marcacao_mod,
}

# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------
_sessions: dict[str, dict] = {}


def _get_or_create(sid: str) -> dict:
    if sid not in _sessions:
        _sessions[sid] = {
            "rows": [],
            "running": False,
            "stop_flag": False,
            "results": [],
            "current_activity": None,
            "ws": None,
            "upload_dir": None,
            "download_dir": None,
        }
    return _sessions[sid]


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return FileResponse(str(_web_dir / "elaw_rpa.html"))


@app.post("/api/session")
async def create_session():
    sid = str(uuid.uuid4())[:12]
    _get_or_create(sid)
    return {"session_id": sid}


@app.post("/api/upload-spreadsheet/{sid}")
async def upload_spreadsheet(sid: str, file: UploadFile = File(...)):
    sess = _get_or_create(sid)
    content = await file.read()
    suffix = Path(file.filename or "planilha.xlsx").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        rows = read_spreadsheet(tmp_path)
        sess["rows"] = rows
        ids = [r["A"] for r in rows]
        return {"ok": True, "filename": file.filename, "total": len(ids), "preview": ids[:5]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.post("/api/upload-documents/{sid}")
async def upload_documents(sid: str, files: list[UploadFile] = File(...)):
    """Recebe arquivos nomeados com o ID da pasta para upload no ELAW."""
    sess = _get_or_create(sid)
    if not sess.get("upload_dir"):
        sess["upload_dir"] = tempfile.mkdtemp()
    saved = []
    for f in files:
        content = await f.read()
        dest = Path(sess["upload_dir"]) / (f.filename or "arquivo")
        dest.write_bytes(content)
        saved.append(f.filename)
    return {"ok": True, "files": saved, "count": len(saved)}


@app.get("/api/download-report/{sid}")
async def download_report(sid: str):
    if sid not in _sessions:
        raise HTTPException(404, "Sessao nao encontrada")
    sess = _sessions[sid]
    if not sess["results"]:
        raise HTTPException(400, "Nenhum resultado disponivel")
    out_dir = Path(tempfile.gettempdir()) / "elaw_relatorios"
    path = reporter.write_report(
        sess["results"],
        sess.get("current_activity") or "execucao",
        out_dir,
    )
    return FileResponse(
        str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/download-docs/{sid}")
async def download_docs(sid: str):
    if sid not in _sessions:
        raise HTTPException(404, "Sessao nao encontrada")
    sess = _sessions[sid]
    dl_dir = sess.get("download_dir")
    if not dl_dir or not Path(dl_dir).exists():
        raise HTTPException(400, "Nenhum documento disponivel")

    files = [f for f in Path(dl_dir).iterdir() if f.is_file()]
    if not files:
        raise HTTPException(400, "Pasta de documentos esta vazia")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=documentos_elaw.zip"},
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws/{sid}")
async def ws_endpoint(websocket: WebSocket, sid: str):
    await websocket.accept()
    sess = _get_or_create(sid)
    sess["ws"] = websocket
    try:
        while True:
            raw = await websocket.receive_text()
            await _handle(sid, json.loads(raw))
    except WebSocketDisconnect:
        if sid in _sessions:
            _sessions[sid]["stop_flag"] = True
            _sessions[sid]["ws"] = None


async def _handle(sid: str, msg: dict):
    sess = _sessions.get(sid)
    if not sess:
        return
    t = msg.get("type")

    if t == "start_run":
        if sess["running"]:
            await _send(sid, {"type": "error", "message": "Execucao ja em andamento"})
            return
        if not sess["rows"]:
            await _send(sid, {"type": "error", "message": "Planilha nao carregada"})
            return
        activity = msg.get("activity")
        if activity not in ACTIVITIES:
            await _send(sid, {"type": "error", "message": f"Atividade invalida: {activity}"})
            return
        sess["running"] = True
        sess["stop_flag"] = False
        sess["results"] = []
        sess["current_activity"] = activity
        loop = asyncio.get_event_loop()
        threading.Thread(
            target=_run_pipeline, args=(sid, msg, loop), daemon=True
        ).start()
        await _send(sid, {"type": "started", "total": len(sess["rows"])})

    elif t == "stop_run":
        sess["stop_flag"] = True
        await _send(sid, {"type": "stopping"})

    elif t == "ping":
        await _send(sid, {"type": "pong", "ts": time.time()})


async def _send(sid: str, data: dict):
    sess = _sessions.get(sid)
    ws = sess and sess.get("ws")
    if ws:
        try:
            await ws.send_json(data)
        except Exception:
            pass


def _send_sync(sid: str, data: dict, loop: asyncio.AbstractEventLoop):
    asyncio.run_coroutine_threadsafe(_send(sid, data), loop)


# ---------------------------------------------------------------------------
# Error helpers (mantidos iguais ao main.py original)
# ---------------------------------------------------------------------------
_FRIENDLY = {
    "abrir_pasta": "Nao consegui abrir a pasta",
    "capa": "Falha ao editar a Capa",
    "prazos": "Falha ao ajustar o Prazo",
    "pedidos": "Falha ao processar o Pedido",
    "documentos": "Falha ao processar Documentos",
    "marcacao": "Falha ao incluir Marcacao",
}


def _friendly(exc, ctx=""):
    raw = str(exc).split("\n")[0].strip()
    base = _FRIENDLY.get(ctx, "Erro")
    if isinstance(exc, RuntimeError):
        # 500 (e nao 200) para nao truncar a lista de campos obrigatorios
        # que a capa devolve quando o save e' bloqueado.
        return (raw or base)[:500]
    if "Timeout" in raw and "exceeded" in raw:
        if "Locator.click" in raw or "locator.click" in raw:
            return base + ": sistema nao respondeu ao clique (timeout)"
        if "wait_for_selector" in raw:
            return base + ": elemento esperado nao apareceu (timeout)"
        if "Page.goto" in raw or "page.goto" in raw:
            return base + ": pagina demorou para carregar (timeout)"
        if "wait_for_url" in raw:
            return base + ": pagina nao mudou apos a acao (timeout)"
        return base + ": tempo esgotado aguardando o sistema"
    if "net::" in raw or "ERR_" in raw:
        return base + ": problema de conexao com o ELAW"
    if "is not visible" in raw or "not actionable" in raw:
        return base + ": elemento nao pode ser usado"
    if not raw:
        return base
    return (base + ": " + raw)[:200]


# ---------------------------------------------------------------------------
# Pipeline (roda em thread separada)
# ---------------------------------------------------------------------------
def _run_pipeline(sid: str, msg: dict, loop: asyncio.AbstractEventLoop):
    def log(line: str):
        _send_sync(sid, {"type": "log", "line": line}, loop)

    def record(idx: int, total: int, pasta_id: str, status: str, detail: str):
        entry = {"idx": idx + 1, "id": pasta_id, "status": status, "detail": detail}
        if sid in _sessions:
            _sessions[sid]["results"].append(entry)
        _send_sync(sid, {
            "type": "progress",
            "idx": idx, "total": total,
            "pasta_id": pasta_id, "status": status, "detail": detail,
        }, loop)
        log(f"[{idx+1:03d}] ID {pasta_id} - {status.upper()}: {detail}")

    def finish(**extra):
        if sid in _sessions:
            _sessions[sid]["running"] = False
            results = _sessions[sid]["results"]
        else:
            results = []
        ok = sum(1 for r in results if r["status"] == "ok")
        err = sum(1 for r in results if r["status"] == "error")
        skip = sum(1 for r in results if r["status"] == "skip")
        payload = {"type": "done", "total": len(results), "ok": ok, "error": err, "skip": skip}
        payload.update(extra)
        _send_sync(sid, payload, loop)

    sess = _sessions.get(sid)
    if not sess:
        return

    activity = msg.get("activity")
    config = dict(msg.get("config", {}))
    creds_dict = msg.get("creds", {})
    activity_mod = ACTIVITIES[activity]
    rows = list(sess["rows"])
    total = len(rows)
    creds = browser_mod.Credentials(
        user=creds_dict.get("user", ""),
        password=creds_dict.get("password", ""),
    )

    # Para documentos: injeta paths gerenciados pelo servidor
    if activity == "documentos":
        if config.get("operacao") == "upload" and sess.get("upload_dir"):
            config["pasta_origem"] = sess["upload_dir"]
        elif config.get("operacao") == "download":
            dl_dir = tempfile.mkdtemp()
            sess["download_dir"] = dl_dir
            config["pasta_destino"] = dl_dir

    log(f"Iniciando '{activity}' para {total} ID(s)")
    started = time.time()

    try:
        _headless = os.environ.get("HEADLESS", "0").lower() not in ("0", "false", "no")
        with browser_mod.ElawSession(creds, headless=_headless, timeout_ms=60000) as browser_sess:

            # Liga callback de debug screenshots apenas se DEBUG_SCREENSHOTS=1.
            # Em producao manter OFF: cada screenshot gera ~200 KB base64 no WebSocket
            # e bloqueia o Chromium por 50-300ms — com 5 shots/caso e 319 casos
            # isso representa ~50 MB de dados e ~8 minutos perdidos so em screenshots.
            _debug_shots_enabled = os.environ.get("DEBUG_SCREENSHOTS", "0") == "1"
            if _debug_shots_enabled:
                def _on_debug_shot(label: str, png: bytes):
                    try:
                        sc = base64.b64encode(png).decode()
                        _send_sync(sid, {"type": "debug_screenshot", "label": label, "data": sc}, loop)
                        log(f"[DEBUG] screenshot '{label}'")
                    except Exception:
                        pass
                browser_sess.debug_screenshot_fn = _on_debug_shot
                log("Debug screenshots LIGADOS (DEBUG_SCREENSHOTS=1)")
            else:
                log("Debug screenshots desligados — defina DEBUG_SCREENSHOTS=1 para investigar bugs")

            try:
                browser_sess.login()
                log("Login no ELAW efetuado com sucesso")
            except Exception as e:
                log(f"ERRO no login: {e}")
                try:
                    png = browser_sess.page.screenshot(timeout=5000)
                    sc = base64.b64encode(png).decode()
                    _send_sync(sid, {"type": "screenshot", "pasta_id": "login", "data": sc}, loop)
                except Exception:
                    pass
                finish(stopped=False, login_error=str(e), elapsed=0)
                return

            # =================================================================
            # ESTRATEGIA DE RECICLAGEM ANTI-MEMORY-LEAK
            # ----------------------------------------------------------------
            # Aba nova por caso: fechada no finally (libera DOM/listeners).
            # Context recycle (cookies/cache + re-login) a cada 50 casos.
            # Browser recycle (Chromium inteiro) a cada 300 casos.
            # Ajuste via env var: RECYCLE_CTX_EVERY, RECYCLE_BROWSER_EVERY
            # =================================================================
            RECYCLE_CTX_EVERY     = int(os.environ.get("RECYCLE_CTX_EVERY",     "50"))
            RECYCLE_BROWSER_EVERY = int(os.environ.get("RECYCLE_BROWSER_EVERY", "300"))
            # Exige 3 falhas consecutivas antes de reciclar o browser
            # (era 2 — muito sensivel a falso-positivos durante navegacoes)
            HEALTH_CHECK_FAILS_BEFORE_BROWSER_RECYCLE = 3

            # Tenta importar psutil para log de memoria (opcional)
            # Mede Python + todos os processos filhos (Chromium incluido)
            try:
                import psutil
                _proc = psutil.Process()
                def _mem_mb():
                    try:
                        total = _proc.memory_info().rss
                        for child in _proc.children(recursive=True):
                            try:
                                total += child.memory_info().rss
                            except Exception:
                                pass
                        return f"{total / (1024*1024):.0f}MB"
                    except Exception:
                        return "?"
            except ImportError:
                LOG.info("psutil nao instalado — log de memoria desativado (pip install psutil)")
                def _mem_mb():
                    return "psutil-off"

            health_fail_count = 0

            for idx, row in enumerate(rows):
                if sess.get("stop_flag"):
                    log("Execucao interrompida pelo usuario")
                    break

                # ---------- RECICLAGEM DE CONTEXTO / BROWSER ----------
                # A aba (Page) e fechada no finally de cada caso — nao precisa
                # de RECYCLE_PAGE. Apenas context e browser reciclam por intervalo.
                if idx > 0 and idx % RECYCLE_BROWSER_EVERY == 0:
                    log(f"[RECYCLE-BROWSER] caso {idx} — relancando Chromium (mem: {_mem_mb()})")
                    try:
                        browser_sess.recycle_browser()
                        log(f"[RECYCLE-BROWSER] OK — mem apos: {_mem_mb()}")
                    except Exception as e:
                        log(f"[ERRO] Falha ao reciclar browser: {e} — abortando run")
                        finish(stopped=False, fatal_error=f"Reciclagem de browser falhou: {e}")
                        return

                elif idx > 0 and idx % RECYCLE_CTX_EVERY == 0:
                    log(f"[RECYCLE-CTX] caso {idx} — novo BrowserContext + re-login (mem: {_mem_mb()})")
                    try:
                        browser_sess.recycle_context()
                        log(f"[RECYCLE-CTX] OK — mem apos: {_mem_mb()}")
                    except Exception as e:
                        log(f"[AVISO] Falha ao reciclar context: {e} — tentando browser recycle")
                        try:
                            browser_sess.recycle_browser()
                        except Exception as e2:
                            log(f"[ERRO] Recycle browser tambem falhou: {e2} — abortando")
                            finish(stopped=False, fatal_error=f"Recycle falhou: {e2}")
                            return

                # ---------- ABA NOVA + HEALTH CHECK ----------
                # recycle_page() ANTES do health_check — caso anterior chamou
                # close_page() que seta self._page=None. Se health_check rodar
                # com self._page=None retorna False imediatamente (falso negativo).
                # Aba em branco e leve: evaluate("1+1") sempre responde rapido.
                browser_sess.recycle_page()

                if not browser_sess.health_check():
                    health_fail_count += 1
                    log(f"[HEALTH] Chromium nao respondeu (falha #{health_fail_count})")
                    if health_fail_count >= HEALTH_CHECK_FAILS_BEFORE_BROWSER_RECYCLE:
                        log(f"[HEALTH] Recriando Chromium apos {health_fail_count} falhas")
                        try:
                            browser_sess.recycle_browser()
                            browser_sess.recycle_page()  # aba limpa pos-recycle
                            health_fail_count = 0
                        except Exception as e:
                            log(f"[ERRO FATAL] Recycle de emergencia falhou: {e}")
                            finish(stopped=False, fatal_error=str(e))
                            return
                else:
                    health_fail_count = 0

                # MEM-ALERT removido: context recycle pos-login estava somando
                # memoria em vez de liberar (619MB -> recycle -> 657MB).
                # Os intervalos RECYCLE_CTX_EVERY / RECYCLE_BROWSER_EVERY
                # ja tratam a limpeza periodica de forma controlada.

                pasta_id = row.get("A", "").strip()
                if not pasta_id:
                    record(idx, total, "", "skip", "ID vazio na linha")
                    continue

                _send_sync(sid, {
                    "type": "progress", "idx": idx, "total": total,
                    "pasta_id": pasta_id, "status": "running", "detail": "Processando...",
                }, loop)
                log(f"[{idx+1:03d}] ID {pasta_id} - buscando pasta (mem: {_mem_mb()})")

                def _screenshot_b64():
                    try:
                        png = browser_sess.page.screenshot(timeout=5000)
                        return base64.b64encode(png).decode()
                    except Exception:
                        return None

                try:
                    browser_sess.open_pasta(pasta_id)

                    # Screenshot de progresso — mostra a tela atual do robô
                    sc = _screenshot_b64()
                    if sc:
                        _send_sync(sid, {
                            "type": "live_screenshot",
                            "pasta_id": pasta_id,
                            "idx": idx + 1,
                            "total": total,
                            "data": sc,
                        }, loop)

                    if activity == "documentos":
                        detail = activity_mod.run(browser_sess.page, config, row, pasta_id=pasta_id)
                    else:
                        detail = activity_mod.run(browser_sess.page, config, row)
                    record(idx, total, pasta_id, "ok", detail)

                except Exception as e:
                    msg_err = _friendly(e, "abrir_pasta" if "pasta" in str(e).lower() else activity)
                    record(idx, total, pasta_id, "error", msg_err)
                    sc = _screenshot_b64()
                    if sc:
                        _send_sync(sid, {"type": "screenshot", "pasta_id": pasta_id, "data": sc}, loop)

                finally:
                    # Fecha a aba — libera toda a memoria do caso imediatamente
                    try:
                        browser_sess.close_page()
                    except Exception:
                        pass

        elapsed = int(time.time() - started)
        dl_dir = sess.get("download_dir")
        has_docs = bool(dl_dir and any(Path(dl_dir).iterdir()))
        finish(
            stopped=sess.get("stop_flag", False),
            elapsed=elapsed,
            has_doc_download=has_docs,
        )

    except Exception as e:
        LOG.error("Erro fatal: %s\n%s", e, traceback.format_exc())
        log(f"ERRO FATAL: {e}")
        finish(stopped=False, fatal_error=str(e))
    finally:
        if sid in _sessions:
            _sessions[sid]["running"] = False
        if sess and sess.get("upload_dir"):
            try:
                shutil.rmtree(sess["upload_dir"], ignore_errors=True)
                sess["upload_dir"] = None
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
