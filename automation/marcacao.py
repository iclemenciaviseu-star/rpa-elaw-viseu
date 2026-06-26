"""
Atividade: Inclusao de Marcacoes (etiquetas).

A "Marcacao" e uma etiqueta do processo (Critico, Estrategico, Sensivel, etc.)
controlada por checkboxes num modal "Editar Marcacoes". O modal e aberto por um
link com title="Editar Marcacoes" que fica num dropdown na aba Geral da pasta.

GOTCHA — iCheck: o componente usa o plugin jQuery iCheck, que ESCONDE o <input>
real sob uma <div class="icheckbox_minimal-red"> estilizada. Setar .checked direto
nao atualiza a UI nem dispara os eventos do plugin. Marcamos via API do iCheck:
    jQuery(cb).iCheck('check')
(confirmado ao vivo em 2026-06-24 — marca o input e a div visual juntos).

POLITICA — SOMENTE ADICIONA: o modal abre refletindo o estado atual da pasta
(etiquetas ja marcadas vem checked). Marcamos apenas as etiquetas indicadas e
nao tocamos nas demais; ao salvar, as que ja existiam sao preservadas. O robo
NUNCA desmarca uma etiqueta.

Config esperada (montada na tela do RPA — mesmas etiquetas para todos os IDs):
{
    "marcacoes": ["Critico", "Estrategico"]   # nomes (labels) das etiquetas
}

Etiquetas do sistema confirmadas em 2026-06-24 (localizadas pelo LABEL, nao pelo
indice, para sobreviver a mudancas de ordem/lista entre processos):
    Advogado Agressor · Encerrado Judicialmente · Critico · Estrategico ·
    Medicina · Sensivel · Honorarios Especiais · Execucao em outro ID
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

LOG = logging.getLogger("elaw.marcacao")

# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------
SEL_CHECKBOX_QUALQUER = "input[type=checkbox][id^='TipoMarcacoes_']"
SEL_TOAST_OK = "#toast-container .toast-success"
SEL_TOAST_ERROR = "#toast-container .toast-error"

# Sinal confiavel de "modal aberto": classe 'in'/'show' ou display != none no
# wrapper .modal. NAO usar offsetParent (modal e position:fixed -> offsetParent
# null mesmo aberto). Confirmado ao vivo: class="modal fade in ...".
_JS_MODAL_ABERTO = """
() => {
    const cb = document.querySelector('input[type=checkbox][id^="TipoMarcacoes_"]');
    const modal = cb ? cb.closest('.modal') : null;
    if (!modal) return false;
    return modal.classList.contains('in') || modal.classList.contains('show')
        || getComputedStyle(modal).display !== 'none';
}
"""


# ---------------------------------------------------------------------------
# JavaScript injetado
# ---------------------------------------------------------------------------

# Abre o modal clicando no link "Editar Marcacoes". O link fica num dropdown que
# pode estar OCULTO — chamamos .click() direto no elemento, que dispara o handler
# do eLaw independente da visibilidade (nao precisamos abrir o dropdown na mao).
_JS_ABRIR = r"""
() => {
    const link = [...document.querySelectorAll('a')].find(a =>
        (a.getAttribute('title') || '').trim() === 'Editar Marcações' ||
        a.textContent.trim() === 'Editar Marcações');
    if (!link) return false;
    link.click();
    return true;
}
"""

# Marca (iCheck check) as etiquetas desejadas, localizando cada uma pelo TEXTO do
# card (.col-md-6). SOMENTE adiciona — nunca desmarca. Retorna o que aconteceu
# para o relatorio (marcadas / ja marcadas / disponiveis / nao encontradas).
_JS_MARCAR = r"""
async (desejados) => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
        .toLowerCase().replace(/\s+/g, ' ').trim();
    const alvo = desejados.map(norm);
    const $ = window.jQuery;
    const cbs = [...document.querySelectorAll('input[type=checkbox][id^="TipoMarcacoes_"]')];
    const out = { marcadas: [], jaMarcadas: [], disponiveis: [], naoEncontradas: [], falhou: [] };

    const labelDe = (cb) => {
        const card = cb.closest('.col-md-6') || cb.closest('.col-sm-6')
            || (cb.parentElement && cb.parentElement.parentElement
                && cb.parentElement.parentElement.parentElement);
        return card ? card.textContent.replace(/\s+/g, ' ').trim() : '';
    };

    const achados = new Set();
    for (const cb of cbs) {
        const label = labelDe(cb);
        if (label) out.disponiveis.push(label);
        if (!alvo.includes(norm(label))) continue;
        achados.add(norm(label));
        if (cb.checked) { out.jaMarcadas.push(label); continue; }
        // Marca com retry: o iCheck pode ignorar o 'check' logo apos o modal
        // abrir (reinicializacao do plugin). Repetimos ate cb.checked confirmar.
        let ok = false;
        for (let t = 0; t < 6 && !ok; t++) {
            if ($ && $.fn && $.fn.iCheck) $(cb).iCheck('check');
            else { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
            if (cb.checked) { ok = true; break; }
            await sleep(200);
        }
        if (ok) out.marcadas.push(label);
        else out.falhou.push(label);
    }
    out.naoEncontradas = desejados.filter(d => !achados.has(norm(d)));
    return out;
}
"""

# Clica no botao Salvar dentro do modal de marcacoes (classe .salvar confirmada).
_JS_SALVAR = r"""
() => {
    const cb = document.querySelector('input[type=checkbox][id^="TipoMarcacoes_"]');
    const cont = cb ? (cb.closest('.modal-content, .modal, form') || document) : document;
    const btn = [...cont.querySelectorAll('button, input[type=submit], input[type=button], a.btn')]
        .find(b => (b.className || '').includes('salvar')
            || /^salvar$/i.test((b.textContent || b.value || '').trim()));
    if (!btn) return false;
    btn.click();
    return true;
}
"""

# Fecha o modal sem salvar (botao Cancelar / X) — usado quando nao ha nada novo.
_JS_CANCELAR = r"""
() => {
    const cb = document.querySelector('input[type=checkbox][id^="TipoMarcacoes_"]');
    const cont = cb ? (cb.closest('.modal-content, .modal') || document) : document;
    const btn = [...cont.querySelectorAll('button, a.btn, button.close, .close')]
        .find(b => /^cancelar$/i.test((b.textContent || '').trim())
            || (b.className || '').includes('close'));
    if (btn) { btn.click(); return true; }
    return false;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _abrir_modal(page: Page) -> None:
    """Abre o modal 'Editar Marcacoes' e aguarda ele ficar VISIVEL.

    MENU "EDITAR" COMPARTILHADO (confirmado 2026-06-24): o link "Editar Marcacoes"
    vive no MESMO dropdown "Editar" do painel da aba Geral de onde a atividade Capa
    pega o item "Editar" (capa.py -> a[href*='/processo/edit/']). A diferenca e o
    DESTINO: "Editar" abre a PAGINA /processo/edit (campos cadastrais — NAO tem os
    checkboxes de marcacao); "Editar Marcacoes" abre ESTE modal. Por isso cada
    atividade aciona o seu proprio item do menu, sem compartilhar o destino — nao
    da para reusar o _abrir_edicao da Capa aqui.

    ATENCAO (confirmado ao vivo 2026-06-24): os 8 checkboxes ficam SEMPRE no DOM
    — o modal vem no HTML da pagina de detalhes, apenas oculto. Por isso nao basta
    esperar o checkbox existir (state="attached" passa de imediato); esperamos o
    modal de fato ABRIR. E nao usamos offsetParent para detectar "visivel": o modal
    Bootstrap usa position:fixed, que zera o offsetParent mesmo aberto. O sinal
    confiavel e a classe 'in'/'show' (ou display != none) no wrapper .modal.
    """
    if not page.evaluate(_JS_ABRIR):
        raise RuntimeError(
            "Link 'Editar Marcacoes' nao encontrado na pasta. "
            "Verifique se a pasta abriu na aba Geral e se ha permissao de edicao."
        )
    try:
        page.wait_for_function(_JS_MODAL_ABERTO, timeout=15_000)
    except PlaywrightTimeoutError as e:
        raise RuntimeError("O modal de Marcacoes nao abriu a tempo (timeout).") from e
    # Folga para o iCheck terminar de pintar os checkboxes
    page.wait_for_timeout(400)


def _fechar_sem_salvar(page: Page) -> None:
    """Best-effort: fecha o modal pelo Cancelar/X (ou ESC). Nunca levanta erro."""
    try:
        if not page.evaluate(_JS_CANCELAR):
            page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception as e:
        LOG.debug("Falha ao fechar modal sem salvar (ignorado): %s", e)


def _confirmar_save(page: Page, timeout_ms: int = 20_000) -> None:
    """Confirma o salvamento.

    O Salvar e um submit POST. Apos salvar, o eLaw fecha o modal (remove os
    checkboxes do DOM) e/ou mostra um toast — em alguns casos a pagina de
    detalhes recarrega. Tratamos como SUCESSO quando: o checkbox some do DOM,
    o modal fecha, aparece toast de sucesso, ou o contexto e destruido por
    navegacao. So falhamos diante de um toast de erro explicito.
    """
    try:
        page.wait_for_function(
            """() => {
                const cb = document.querySelector('input[type=checkbox][id^="TipoMarcacoes_"]');
                const modal = cb ? cb.closest('.modal') : null;
                const modalAberto = modal
                    ? (modal.classList.contains('in') || modal.classList.contains('show')
                       || getComputedStyle(modal).display !== 'none')
                    : false;
                const ok  = document.querySelector('#toast-container .toast-success');
                const err = document.querySelector('#toast-container .toast-error');
                return !modalAberto || !!ok || !!err;
            }""",
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        pass  # sem sinal claro — verifica erro explicito abaixo
    except Exception as e:
        # Navegacao (reload pos-POST) destroi o contexto de execucao = sucesso
        if "context" in str(e).lower() or "destroyed" in str(e).lower():
            LOG.info("Contexto destruido apos salvar — pagina recarregou (sucesso).")
            return
        raise

    try:
        if page.locator(SEL_TOAST_ERROR).count() > 0:
            msg = page.locator(SEL_TOAST_ERROR + " .toast-message").first.inner_text(timeout=2_000)
            raise RuntimeError(f"Erro ao salvar marcacoes: {msg.strip()[:200]}")
    except PlaywrightTimeoutError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(page: Page, config: dict, row: dict) -> str:
    """Adiciona as etiquetas indicadas a pasta ja aberta. Retorna o detalhe."""
    desejadas = [str(m).strip() for m in (config.get("marcacoes") or []) if str(m).strip()]
    if not desejadas:
        raise RuntimeError("Nenhuma etiqueta de marcacao selecionada na configuracao")

    _abrir_modal(page)

    res = page.evaluate(_JS_MARCAR, desejadas)
    marcadas = res.get("marcadas", [])
    ja = res.get("jaMarcadas", [])
    nao = res.get("naoEncontradas", [])
    falhou = res.get("falhou", [])

    # iCheck nao confirmou a selecao mesmo apos retry -> falha honesta
    # (melhor errar do que reportar sucesso sem ter marcado de fato)
    if falhou:
        raise RuntimeError(
            "Nao consegui confirmar a marcacao de: " + ", ".join(falhou)
            + " (o iCheck nao aplicou apos varias tentativas)."
        )

    # Nenhuma das desejadas existe nesta pasta -> erro de configuracao
    if not marcadas and not ja:
        disp = ", ".join([d for d in res.get("disponiveis", []) if d][:12])
        _fechar_sem_salvar(page)
        raise RuntimeError(
            f"Etiqueta(s) '{', '.join(desejadas)}' nao existe(m) nesta pasta. "
            f"Disponiveis: {disp or '(nenhuma)'}"
        )

    # Tudo ja estava marcado -> idempotente, fecha sem submeter
    if not marcadas:
        _fechar_sem_salvar(page)
        return f"Nenhuma alteracao — ja estavam marcadas: {', '.join(ja)}"

    if not page.evaluate(_JS_SALVAR):
        raise RuntimeError("Botao Salvar das marcacoes nao encontrado no modal")
    _confirmar_save(page)

    detalhe = f"Marcacoes adicionadas: {', '.join(marcadas)}"
    if ja:
        detalhe += f" (ja marcadas: {', '.join(ja)})"
    if nao:
        detalhe += f" (inexistentes ignoradas: {', '.join(nao)})"
    return detalhe
