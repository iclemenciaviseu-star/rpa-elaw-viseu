"""
Atividade: Ajuste de Prazos.

Fluxo confirmado via DevTools:
  1. Aba Prazos (#box-compromissos) lista os agendamentos.
  2. Cada linha tem APENAS um link "Detalhes" -> /compromisso/details/{id}.
     Nao existe botao Editar na propria linha.
  3. Para alterar, navega-se ate /compromisso/details/{id} e clica em
     #buttonEditCompromisso, o que coloca a tela em modo edicao.
  4. Em modo edicao:
       - #DataCompromisso        -> "Data do Prazo Interno"
       - #DataPrazoFatalManual   -> "Prazo Fatal" (manual)
       - #DataHoraPrazoAutomatico -> Prazo Fatal automatico (read-only normalmente)
  5. Botao Salvar e um <button class="btn btn-primary">Salvar</button>
     (sem id) — selecionado pelo texto.

Config esperada:
{
    "subtipo": "Confirmar Habilitacao",   # nome exato do agendamento (Sub. Tipo)
    "prazo_tipo": "prazoInterno",          # ou "prazoFatal"
    "coluna_data": "B",                     # letra da coluna na planilha
}
"""

from __future__ import annotations

import logging
import re
from typing import Any

from playwright.sync_api import Page

LOG = logging.getLogger("elaw.prazos")

# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
SEL_TAB_PRAZOS = "a[data-toggle='tab'][href='#box-compromissos']"

# Link "Detalhes" na linha do agendamento (vai para /compromisso/details/{id})
SEL_LINK_DETALHES_NA_LINHA = "a[href*='/compromisso/details/']"

# Botao "Alterar" dentro da pagina de detalhes do prazo
SEL_BTN_EDITAR_COMPROMISSO = "#buttonEditCompromisso"

# Inputs de data do agendamento (em modo edicao)
SEL_INPUT_PRAZO_INTERNO = "#DataCompromisso"
SEL_INPUT_PRAZO_FATAL = "#DataPrazoFatalManual"

# Botao Salvar nao tem id - usamos o texto
SEL_BTN_SALVAR_PRAZO = "button.btn-primary:has-text('Salvar')"
SEL_TOAST_OK = "#toast-container .toast-success"  # confirmado: toastr
SEL_TOAST_ERROR = "#toast-container .toast-error"

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _row_selector(subtipo: str) -> str:
    """Linha (<tr>) do agendamento cujo subtipo bate com o texto."""
    return (
        f"xpath=//div[@id='box-compromissos']"
        f"//tr[td[normalize-space()='{subtipo}']]"
    )


def _normalize_date(raw: Any) -> str:
    """Garante formato dd/mm/aaaa. Aceita datetime, str ISO etc."""
    s = str(raw).strip()
    if DATE_RE.match(s):
        return s
    for sep in ("-", "."):
        if sep in s and len(s) >= 10:
            parts = s[:10].split(sep)
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    return f"{parts[2]}/{parts[1]}/{parts[0]}"
                return f"{parts[0]}/{parts[1]}/{parts[2]}"
    raise RuntimeError(f"Data em formato invalido: '{raw}' (esperado dd/mm/aaaa)")


def run(page: Page, config: dict, row: dict) -> str:
    subtipo = (config.get("subtipo") or "").strip()
    prazo_tipo = config.get("prazo_tipo", "prazoInterno")
    coluna = (config.get("coluna_data") or "").strip().upper()

    if not subtipo:
        raise RuntimeError("Subtipo de agendamento nao configurado")
    if not coluna:
        raise RuntimeError("Coluna da nova data nao configurada")

    nova_data = _normalize_date(row.get(coluna, ""))

    # 1. Aba Prazos
    if page.locator(SEL_TAB_PRAZOS).count() > 0:
        page.locator(SEL_TAB_PRAZOS).first.click()
        try:
            page.wait_for_selector("#box-compromissos tbody tr", timeout=5000)
        except Exception:
            pass

    # 2. Localiza linha do subtipo
    rows = page.locator(_row_selector(subtipo))
    n = rows.count()
    if n == 0:
        raise RuntimeError(f"Subtipo '{subtipo}' nao encontrado na aba Prazos")
    if n > 1:
        raise RuntimeError(f"Mais de um agendamento com subtipo '{subtipo}'")

    # 3. Clica no link "Detalhes" -> abre /compromisso/details/{id}
    rows.first.locator(SEL_LINK_DETALHES_NA_LINHA).first.click()
    page.wait_for_selector(SEL_BTN_EDITAR_COMPROMISSO, timeout=10000)

    # 4. Clica em "Alterar"
    page.click(SEL_BTN_EDITAR_COMPROMISSO)
    sel_input = SEL_INPUT_PRAZO_INTERNO if prazo_tipo == "prazoInterno" else SEL_INPUT_PRAZO_FATAL
    page.wait_for_selector(sel_input, state="visible", timeout=10000)

    # 5. Preenche o campo certo
    page.fill(sel_input, nova_data)

    # 6. Salva
    page.click(SEL_BTN_SALVAR_PRAZO)
    try:
        page.wait_for_selector(f"{SEL_TOAST_OK}, {SEL_TOAST_ERROR}", timeout=10000)
        if page.locator(SEL_TOAST_ERROR).count() > 0:
            msg = page.locator(SEL_TOAST_ERROR + " .toast-message").first.inner_text()
            raise RuntimeError(f"Erro no Prazo: {msg.strip()[:200]}")
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise
        # Fallback: aguarda saida do modo edicao (botao Alterar reaparece)
        page.wait_for_selector(SEL_BTN_EDITAR_COMPROMISSO, timeout=10000)

    label = "Prazo Interno" if prazo_tipo == "prazoInterno" else "Prazo Fatal"
    return f"{label} de '{subtipo}' atualizado para {nova_data}"
