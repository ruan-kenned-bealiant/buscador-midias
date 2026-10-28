/*
 * Ajudantes de interface compartilhados pelas telas do painel.
 * Expõe window.UI com track(), busy() e skeleton(); toda chamada a fetch() passa por track(),
 * então a barra de progresso aparece sozinha em qualquer carregamento mais lento.
 */
(() => {
  'use strict';
  const SHOW_AFTER_MS = 300;
  let pending = 0;
  let timer = 0;

  const bar = document.createElement('div');
  bar.className = 'page-progress';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-label', 'Carregando');
  bar.setAttribute('aria-hidden', 'true');
  document.body.prepend(bar);

  /** Liga a barra só se a espera passar de SHOW_AFTER_MS, para consultas rápidas não piscarem. */
  function settle() {
    clearTimeout(timer);
    if (pending > 0) {
      timer = setTimeout(() => document.documentElement.classList.add('is-loading'), SHOW_AFTER_MS);
    } else {
      document.documentElement.classList.remove('is-loading');
    }
  }

  /** Acompanha uma promessa: conta como carregamento até ela terminar, com sucesso ou erro. */
  function track(promise) {
    pending += 1;
    if (pending === 1) settle();
    const done = () => {
      pending = Math.max(0, pending - 1);
      if (pending === 0) settle();
    };
    promise.then(done, done);
    return promise;
  }

  /** Liga ou desliga o estado ocupado de um botão (indicador, sem novo clique, leitor de tela avisado). */
  function mark(button, on) {
    button.classList.toggle('is-busy', on);
    button.disabled = on;
    if (on) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
  }

  /** Mantém o botão ocupado enquanto a promessa não termina. */
  function busy(button, promise) {
    mark(button, true);
    promise.then(() => mark(button, false), () => mark(button, false));
    return promise;
  }

  /**
   * Preenche o contêiner com blocos que ocupam o lugar do conteúdo.
   * kind: 'row' (linha de lista), 'card' (cartão) ou 'line' (linhas de texto).
   */
  function skeleton(container, count = 4, kind = 'row') {
    const nodes = [];
    for (let i = 0; i < count; i += 1) {
      const block = document.createElement('div');
      block.setAttribute('aria-hidden', 'true');
      block.className = kind === 'card' ? 'skeleton-card' : kind === 'row' ? 'skeleton-row' : '';
      for (const width of kind === 'line' ? ['', 'mid', 'short'] : ['mid', 'short']) {
        const line = document.createElement('span');
        line.className = ('skeleton line ' + width).trim();
        block.append(line);
      }
      nodes.push(block);
    }
    container.replaceChildren(...nodes);
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = (...args) => track(nativeFetch(...args));
  window.UI = { track, busy, mark, skeleton };
})();
