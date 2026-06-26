"""
Atividade: Documentos.

Upload (importar) ou Download (exportar) de documentos da aba Documentos.

Fluxo confirmado via DevTools:

UPLOAD:
  1. Aba Documentos (#box-documentos)
  2. Clica #buttonNewDocumento
  3. Abre modal #dialog-modal com:
       - <select id="FullProcessoId"> -> "Fase" (categoria do documento)
       - <input id="ufile" type="file" multiple> -> arquivos
  4. Clica em <button class="btn-primary">Salvar</button> dentro do modal

DOWNLOAD:
  1. Aba Documentos
  2. Extrai lista de documentos via JavaScript (id, nome, tipo)
  3. Seleciona o primeiro documento que bate com alguma das prioridades (em ordem)
  4. Faz download direto via HTTP usando os cookies da sessao Playwright

Config esperada (UPLOAD):
{
    "operacao": "upload",
    "pasta_origem": "C:/Documentos/Upload",  # injetado pelo servidor
    "tipo_documento": "Inicial",
}

Config esperada (DOWNLOAD):
{
    "operacao": "download",
    "pasta_destino": "C:/Documentos/Download",  # injetado pelo servidor
    "prioridades": ["Peticao Inicial", "Contestacao", "Habilitacao", "Inicial"],
}

O arquivo e nomeado com o ID/numero pesquisado. Caracteres invalidos para o
Windows (/ \ : * ? " < > |) sao substituidos por "." para permitir salvar.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

import requests

from playwright.sync_api import Page

LOG = logging.getLogger("elaw.documentos")

# ---------------------------------------------------------------------------
# Selectors (todos confirmados via DevTools)
# ---------------------------------------------------------------------------
SEL_TAB_DOCUMENTOS = "a[data-toggle='tab'][href='#box-documentos']"

# UPLOAD
SEL_BTN_NOVO_DOC = "#buttonNewDocumento"
SEL_MODAL = "#dialog-modal"
SEL_INPUT_FILE = "#ufile"
SEL_SELECT_FASE = "#FullProcessoId"
SEL_BTN_SALVAR_DOC = f"{SEL_MODAL} button.btn-primary"
SEL_TOAST_OK = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"

# JavaScript para extrair documentos da tabela (portado de rpa_elaw.py)
_JS_OBTER_DOCS = """
() => {
    var resultado = [];
    var box = document.getElementById('box-documentos');
    if (!box) return resultado;
    var rows = box.querySelectorAll('tr');
    for (var i = 0; i < rows.length; i++) {
        var tds = rows[i].querySelectorAll('td');
        if (tds.length < 8) continue;
        var docId    = (tds[3].innerText || '').trim();
        var nome     = (tds[4].innerText || '').trim();
        var extensao = (tds[6].innerText || '').trim();
        var tipo     = (tds[7].innerText || '').trim();
        if (!docId || isNaN(docId)) continue;
        resultado.push({doc_id: docId, nome: nome, extensao: extensao, tipo: tipo});
    }
    return resultado;
}
"""

_MIME_EXT = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


_INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str) -> str:
    return _INVALID_FS.sub(".", name).strip() or "arquivo"


def normalizar(texto: str) -> str:
    """Remove acentos e converte para maiusculas para comparacao insensivel."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto)
        if unicodedata.category(c) != 'Mn'
    ).upper().strip()


def _selecionar_por_prioridade(docs: list[dict], prioridades: list[str]) -> dict | None:
    """
    Itera as prioridades em ordem e retorna o primeiro documento da lista
    cujo tipo ou nome contenha a prioridade normalizada.
    """
    for p in prioridades:
        p_norm = normalizar(p)
        if not p_norm:
            continue
        for doc in docs:
            if p_norm in normalizar(doc.get("tipo", "")) or p_norm in normalizar(doc.get("nome", "")):
                return doc
    return None


def _list_arquivos_para_id(pasta: Path, pasta_id: str) -> list[Path]:
    """Encontra arquivos cujo nome (sem extensao) bate com o ID da pasta."""
    if not pasta.is_dir():
        raise RuntimeError(f"Pasta de origem nao existe: {pasta}")
    return [p for p in pasta.iterdir() if p.is_file() and p.stem == str(pasta_id)]


def _aguardar_modal(page: Page) -> None:
    page.wait_for_selector(f"{SEL_MODAL}.in, {SEL_MODAL}.show", timeout=10000)
    page.wait_for_selector(SEL_INPUT_FILE, state="attached", timeout=10000)


def _upload(page: Page, config: dict, pasta_id: str) -> str:
    pasta_origem = Path(str(config.get("pasta_origem", ""))).expanduser()
    tipo_doc = (config.get("tipo_documento") or "").strip()

    arquivos = _list_arquivos_para_id(pasta_origem, pasta_id)
    if not arquivos:
        raise RuntimeError(f"Nenhum arquivo nomeado '{pasta_id}.*' em {pasta_origem}")

    page.click(SEL_BTN_NOVO_DOC)
    _aguardar_modal(page)

    if tipo_doc:
        try:
            page.locator(SEL_SELECT_FASE).first.select_option(label=tipo_doc)
        except Exception:
            btn = page.locator(
                f"{SEL_SELECT_FASE} + button.dropdown-toggle, {SEL_SELECT_FASE} + .dropdown-toggle"
            )
            if btn.count() > 0:
                btn.first.click()
                page.locator(f".dropdown-menu li a:has-text('{tipo_doc}')").first.click()
            else:
                raise

    page.set_input_files(SEL_INPUT_FILE, [str(a) for a in arquivos])
    page.click(SEL_BTN_SALVAR_DOC)

    try:
        page.wait_for_selector(f"{SEL_TOAST_OK}, {SEL_TOAST_ERROR}", timeout=15000)
        if page.locator(SEL_TOAST_ERROR).count() > 0:
            msg = page.locator(SEL_TOAST_ERROR + " .toast-message").first.inner_text()
            raise RuntimeError(f"Erro no upload: {msg.strip()[:200]}")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        page.wait_for_selector(SEL_MODAL, state="hidden", timeout=15000)

    return f"Upload de {len(arquivos)} arquivo(s): {', '.join(a.name for a in arquivos)}"


def _download(page: Page, config: dict, pasta_id: str, row: dict) -> str:
    pasta_destino = Path(str(config.get("pasta_destino", ""))).expanduser()
    pasta_destino.mkdir(parents=True, exist_ok=True)
    prioridades: list[str] = config.get("prioridades") or []

    # Extrai documentos da tabela via JavaScript (portado de rpa_elaw.py)
    docs: list[dict] = page.evaluate(_JS_OBTER_DOCS)
    if not docs:
        raise RuntimeError("Nenhum documento encontrado na pasta")

    tipos_log = ', '.join(d.get("tipo") or d.get("nome") or "?" for d in docs)
    LOG.info("Documentos encontrados: %s", tipos_log)

    # Seleciona por prioridade ou usa o primeiro disponivel
    if prioridades:
        doc_sel = _selecionar_por_prioridade(docs, prioridades)
        if not doc_sel:
            return f"Nenhuma prioridade encontrada. Disponíveis: {tipos_log}"
    else:
        doc_sel = docs[0]

    tipo_sel = doc_sel.get("tipo") or doc_sel.get("nome")
    doc_id = doc_sel["doc_id"]
    LOG.info("Documento selecionado: %s (ID: %s)", tipo_sel, doc_id)

    # Verifica se já existe arquivo com esse nome (qualquer extensao)
    nome_sanitizado = _safe_filename(pasta_id)
    for f in pasta_destino.iterdir():
        if f.is_file() and f.stem == nome_sanitizado:
            return f"Arquivo já existe: {f.name} — pulando"

    # Extrai cookies da sessao Playwright e URL base da pagina atual
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    parsed = urlparse(page.url)
    url_base = f"{parsed.scheme}://{parsed.netloc}"
    url_download = f"{url_base}/documento/download/{doc_id}"

    try:
        resposta = requests.get(url_download, cookies=cookies, stream=True, timeout=120)
        resposta.raise_for_status()

        content_type = resposta.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise RuntimeError("Sessão expirada ou acesso negado (resposta HTML)")

        # Determina extensao pelo Content-Disposition ou Content-Type
        ext = ".pdf"
        cd = resposta.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            nome_header = cd.split("filename=")[-1].strip().strip('"').strip("'")
            _, ext_header = os.path.splitext(nome_header)
            if ext_header:
                ext = ext_header.lower()
        else:
            for mime, e in _MIME_EXT.items():
                if mime in content_type:
                    ext = e
                    break

        destino = pasta_destino / (nome_sanitizado + ext)
        with open(destino, "wb") as f:
            for chunk in resposta.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        return f"Download: {destino.name} (tipo: {tipo_sel})"

    except requests.exceptions.Timeout:
        raise RuntimeError("Timeout: servidor não respondeu em 120 segundos")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Erro HTTP {e.response.status_code}: {e}")


def run(page: Page, config: dict, row: dict, pasta_id: str = "") -> str:
    if page.locator(SEL_TAB_DOCUMENTOS).count() > 0:
        page.locator(SEL_TAB_DOCUMENTOS).first.click()
        try:
            page.wait_for_selector("#box-documentos tbody tr", timeout=5000)
        except Exception:
            pass

    operacao = config.get("operacao", "upload")
    if operacao == "upload":
        return _upload(page, config, pasta_id)
    return _download(page, config, pasta_id, row)
