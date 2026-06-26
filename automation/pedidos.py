"""
Atividade: Pedidos.

Cria ou edita pedidos na aba "Pedidos" da pasta.
Os formularios de criar/editar pedido aparecem em um MODAL (#dialog-modal).

Config esperada:
{
    "operacao": "criar" | "editar",

    # Apenas para "editar":
    "de":   "Total do Pedido",   # tipo atual (texto exato como aparece na tabela)
    "para": "Danos Morais",      # novo tipo

    # Colunas da planilha (letra, ex: "B"):
    "colunas": {
        "tipo":       "B",   # Tipo do Pedido   (TipoPedidoId)
        "valor":      "C",   # Valor do Pedido  (Valor)
        "data":       "D",   # Data do Pedido   (DataPedido) — formato dd/mm/yyyy
        "valor_prov": "E",   # Valor a Provisionar (ValorProvisao)
        "risco":      "F",   # Risco            (ProvisaoId)
        "data_juros": "G",   # Data Base Calculo Juros (DataBaseCalculoId)
    }
}

Campos obrigatorios pelo ELAW: TipoPedidoId, DataPedido, ProvisaoId,
DataBaseCalculoId, Valor, PercentualJurosId, IndiceId.
Deixe em branco os que nao precisar alterar.
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

LOG = logging.getLogger("elaw.pedidos")

# ---------------------------------------------------------------------------
# Selectors (confirmados via ficha tecnica e DevTools)
# ---------------------------------------------------------------------------
SEL_TAB_PEDIDOS    = "a[data-toggle='tab'][href='#box-pedidos']"
SEL_BOX_PEDIDOS    = "#box-pedidos"

# Botoes da aba
SEL_BTN_NOVO_PEDIDO   = "#buttonNewPedido"
SEL_BTN_EDITAR_LINHA  = "a.buttonEditPedido"

# Modal compartilhado do ELAW
SEL_MODAL         = "#dialog-modal"
SEL_MODAL_ABERTO  = "#dialog-modal.in, #dialog-modal.show"
# Ficha §14.2: botao Salvar e button.btn-primary.salvar no .modal-footer
SEL_BTN_SALVAR    = "#dialog-modal .modal-footer button.btn-primary, #dialog-modal .modal-footer button.salvar"

# Campos do modal (ficha §14.1)
SEL_SELECT_TIPO      = "#TipoPedidoId"
SEL_INPUT_DATA       = "#DataPedido"
SEL_SELECT_RISCO     = "#ProvisaoId"
SEL_INPUT_VALOR      = "#Valor"
SEL_INPUT_VALOR_PROV = "#ValorProvisao"
SEL_SELECT_DATA_JUROS = "#DataBaseCalculoId"

# Toasts (ficha §10 armadilha #3)
SEL_TOAST_OK    = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _get(row: dict, col_letter: str) -> str:
    """Extrai valor de uma coluna da linha da planilha."""
    if not col_letter:
        return ""
    return str(row.get(str(col_letter).strip().upper(), "")).strip()


def _aguardar_ajax(page: Page, timeout_ms: int = 15_000):
    """Aguarda overlay blockUI desaparecer apos acao que dispara AJAX.

    Selects dependentes (ex.: Tipo → carrega subcampos) fazem requisicao
    AJAX. E preciso esperar o carregamento terminar antes de prosseguir.
    """
    try:
        page.wait_for_selector(".blockUI, .blockOverlay", state="attached", timeout=2_000)
        LOG.debug("blockUI detectado — aguardando fim do AJAX...")
    except Exception:
        pass
    for sel in [".blockUI", ".blockOverlay"]:
        try:
            page.wait_for_selector(sel, state="detached", timeout=timeout_ms)
            LOG.debug("blockUI sumiu ('%s')", sel)
            return
        except Exception:
            pass
    LOG.debug("blockUI nao detectado — prosseguindo")


def _select_value(page: Page, sel: str, value: str) -> None:
    """Seleciona um valor em <select> e dispara eventos de plugin.

    Suporta bootstrap-select, select2 e select nativo.
    Apos selecionar, aguarda AJAX de campos dependentes.
    """
    loc = page.locator(sel).first

    # Tenta pelo label visivel; fallback pelo atributo value
    try:
        loc.select_option(label=value)
    except Exception:
        try:
            loc.select_option(value=value)
        except Exception as e:
            raise RuntimeError(
                f"Select '{sel}': opcao '{value}' nao encontrada. "
                f"Verifique se o valor e exatamente como aparece no sistema."
            ) from e

    # Dispara eventos e atualiza widgets visuais dos plugins jQuery
    page.evaluate(
        """(el) => {
            el.dispatchEvent(new Event('change', {bubbles: true}));
            if (window.jQuery) {
                try { window.jQuery(el).trigger('change'); }             catch(e) {}
                try { window.jQuery(el).trigger('change.select2'); }     catch(e) {}
                try {
                    if (window.jQuery.fn.selectpicker)
                        window.jQuery(el).selectpicker('refresh');
                } catch(e) {}
                try {
                    if (window.jQuery.fn.multiselect)
                        window.jQuery(el).multiselect('refresh');
                } catch(e) {}
            }
        }""",
        loc.element_handle(),
    )

    # Aguarda AJAX de campos dependentes
    _aguardar_ajax(page, timeout_ms=15_000)
    page.wait_for_timeout(1_000)


def _fill_text(page: Page, sel: str, value: str) -> None:
    """Preenche campo de texto digitando caracter a caracter.

    Ficha §14.3: campos de moeda usam mascara (jquery.maskMoney).
    Digitar devagar garante que a mascara processe cada digito.
    """
    loc = page.locator(sel).first
    loc.click(timeout=5_000)
    loc.fill("")
    loc.type(value, delay=60)
    page.evaluate(
        """(el) => {
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur',   {bubbles: true}));
        }""",
        loc.element_handle(),
    )
    page.wait_for_timeout(500)


def _aguardar_modal(page: Page) -> None:
    """Aguarda o modal abrir completamente e estar pronto para interacao."""
    # Espera o modal ter a classe .in ou .show (Bootstrap 3/4)
    try:
        page.wait_for_selector(SEL_MODAL_ABERTO, timeout=10_000)
    except PlaywrightTimeoutError:
        # Tenta fallback: modal visivel sem a classe .in
        page.wait_for_selector(f"{SEL_MODAL}:visible", timeout=5_000)

    # Espera o botao Salvar do modal estar visivel (confirma que o form carregou)
    page.wait_for_selector(SEL_BTN_SALVAR, state="visible", timeout=10_000)

    # Pausa extra para animacao de abertura terminar
    page.wait_for_timeout(800)
    LOG.info("Modal aberto e pronto.")


def _preencher_campos_modal(page: Page, row: dict, colunas: dict) -> dict:
    """Preenche os campos do modal com os valores das colunas da planilha.

    Preenche apenas os campos com colunas configuradas e valores nao-vazios.
    Retorna dict com os valores efetivamente preenchidos.
    """
    aplicados = {}

    tipo = _get(row, colunas.get("tipo", ""))
    if tipo:
        LOG.info("Preenchendo Tipo do Pedido: '%s'", tipo)
        _select_value(page, SEL_SELECT_TIPO, tipo)
        aplicados["tipo"] = tipo
        page.wait_for_timeout(1_500)

    data = _get(row, colunas.get("data", ""))
    if data:
        LOG.info("Preenchendo Data do Pedido: '%s'", data)
        _fill_text(page, SEL_INPUT_DATA, data)
        aplicados["data"] = data

    risco = _get(row, colunas.get("risco", ""))
    if risco:
        LOG.info("Preenchendo Risco: '%s'", risco)
        _select_value(page, SEL_SELECT_RISCO, risco)
        aplicados["risco"] = risco
        page.wait_for_timeout(1_500)

    valor = _get(row, colunas.get("valor", ""))
    if valor:
        LOG.info("Preenchendo Valor do Pedido: '%s'", valor)
        _fill_text(page, SEL_INPUT_VALOR, valor)
        aplicados["valor"] = valor

    valor_prov = _get(row, colunas.get("valor_prov", ""))
    if valor_prov:
        LOG.info("Preenchendo Valor a Provisionar: '%s'", valor_prov)
        _fill_text(page, SEL_INPUT_VALOR_PROV, valor_prov)
        aplicados["valor_prov"] = valor_prov

    data_juros = _get(row, colunas.get("data_juros", ""))
    if data_juros:
        LOG.info("Preenchendo Data Base Calculo Juros: '%s'", data_juros)
        _select_value(page, SEL_SELECT_DATA_JUROS, data_juros)
        aplicados["data_juros"] = data_juros

    return aplicados


def _aguardar_jquery_idle(page: Page, timeout_ms: int = 5_000) -> None:
    """Aguarda jQuery.active == 0 — sem AJAX pendente no ELAW."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        ativo = page.evaluate(
            "() => (typeof jQuery !== 'undefined') ? jQuery.active : 0"
        )
        if ativo == 0:
            return
        page.wait_for_timeout(150)


def _salvar_modal(page: Page) -> None:
    """Clica em Salvar no modal e aguarda confirmacao."""
    # Aguarda jQuery.active == 0 em vez de sleep fixo de 30s
    LOG.info("Aguardando estabilizacao pre-save (jQuery.active)...")
    _aguardar_jquery_idle(page, timeout_ms=8_000)
    page.wait_for_timeout(800)
    LOG.info("Clicando em Salvar.")

    # Clica via JS para evitar interferencia de scroll/blur
    clicou = page.evaluate(
        f"""() => {{
            // Tenta pelo seletor especifico do modal
            const sel = '{SEL_BTN_SALVAR}'.split(',')[0].trim();
            const btn = document.querySelector(sel) ||
                        document.querySelector('#dialog-modal .modal-footer button');
            if (btn) {{ btn.click(); return true; }}
            return false;
        }}"""
    )
    if not clicou:
        # Fallback: Playwright click direto
        page.locator(SEL_BTN_SALVAR).first.click(timeout=5_000)

    LOG.info("Salvar clicado.")

    # Aguarda toast ou fechamento do modal
    try:
        page.wait_for_selector(
            f"{SEL_TOAST_OK}, {SEL_TOAST_ERROR}",
            timeout=15_000,
        )
        if page.locator(SEL_TOAST_ERROR).count() > 0:
            try:
                msg = (
                    page.locator(SEL_TOAST_ERROR + " .toast-message")
                    .first.inner_text(timeout=3_000)
                )
                raise RuntimeError(f"Erro ao salvar pedido: {msg.strip()[:300]}")
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError("Erro ao salvar pedido (toast de erro detectado)")
        LOG.info("Pedido salvo com sucesso (toast de sucesso).")
    except PlaywrightTimeoutError:
        # Sem toast — espera o modal fechar como indicador de sucesso
        try:
            page.wait_for_selector(SEL_MODAL, state="hidden", timeout=10_000)
            LOG.info("Pedido salvo (modal fechou).")
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "Timeout aguardando confirmacao do salvamento do pedido. "
                "Verifique se ha campos obrigatorios nao preenchidos."
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(page: Page, config: dict, row: dict) -> str:
    """Executa a atividade de Pedidos para um ID. Retorna mensagem de detalhe."""

    operacao = (config.get("operacao") or "criar").strip().lower()
    colunas  = config.get("colunas") or {}

    # 1. Navega para a aba Pedidos
    tab = page.locator(SEL_TAB_PEDIDOS)
    if tab.count() > 0:
        tab.first.click()
        LOG.info("Aba Pedidos clicada.")
        # Aguarda AJAX de carregamento da aba
        _aguardar_ajax(page, timeout_ms=15_000)
        page.wait_for_timeout(1_000)
    else:
        LOG.warning("Aba Pedidos nao encontrada — pode ja estar ativa.")

    # Confirma que o conteudo da aba carregou
    try:
        page.wait_for_selector(SEL_BOX_PEDIDOS, state="visible", timeout=10_000)
    except Exception:
        LOG.warning("Container #box-pedidos nao ficou visivel — prosseguindo mesmo assim.")

    # -----------------------------------------------------------------------
    # CRIAR
    # -----------------------------------------------------------------------
    if operacao == "criar":
        LOG.info("Operacao: CRIAR novo pedido")
        page.locator(SEL_BTN_NOVO_PEDIDO).first.click()
        _aguardar_modal(page)

        aplicados = _preencher_campos_modal(page, row, colunas)
        if not aplicados:
            raise RuntimeError(
                "Nenhum campo foi preenchido — configure ao menos uma coluna na planilha."
            )

        _salvar_modal(page)
        partes = ", ".join(f"{k}={v!r}" for k, v in aplicados.items())
        return f"Pedido criado: {partes}"

    # -----------------------------------------------------------------------
    # EDITAR
    # -----------------------------------------------------------------------
    if operacao == "editar":
        de   = (config.get("de")   or "").strip()
        para = (config.get("para") or "").strip()
        if not de:
            raise RuntimeError("Operacao 'editar' requer o campo 'de' (tipo atual do pedido)")

        LOG.info("Operacao: EDITAR pedido '%s'", de)

        # Localiza a linha pelo texto do Tipo do Pedido na tabela
        linha_sel = (
            f"xpath=//div[@id='box-pedidos']"
            f"//tr[td[contains(normalize-space(),'{de}')]]"
        )
        linhas = page.locator(linha_sel)
        if linhas.count() == 0:
            raise RuntimeError(
                f"Pedido com tipo '{de}' nao encontrado na aba Pedidos desta pasta."
            )

        # Clica no botao Editar da linha
        linhas.first.locator(SEL_BTN_EDITAR_LINHA).first.click()
        _aguardar_modal(page)

        # Se informou "para", troca o tipo primeiro
        if para:
            LOG.info("Alterando tipo: '%s' -> '%s'", de, para)
            _select_value(page, SEL_SELECT_TIPO, para)
            page.wait_for_timeout(1_500)

        # Preenche demais campos configurados
        aplicados = _preencher_campos_modal(page, row, colunas)
        if para:
            aplicados["tipo"] = para

        _salvar_modal(page)
        partes = ", ".join(f"{k}={v!r}" for k, v in aplicados.items())
        return f"Pedido editado ('{de}' → '{para or de}'): {partes}"

    raise RuntimeError(f"Operacao '{operacao}' desconhecida. Use 'criar' ou 'editar'.")
