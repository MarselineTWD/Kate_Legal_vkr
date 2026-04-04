(function () {
  const chat = document.getElementById('floating-chat');
  const closeBtn = document.getElementById('floating-chat-close');
  if (!chat || !closeBtn) return;

  const chatKey = chat.dataset.chatKey || 'default-chat';
  const storageKey = 'chat_shown_' + chatKey;

  if (!sessionStorage.getItem(storageKey)) {
    setTimeout(() => {
      chat.classList.add('show');
      sessionStorage.setItem(storageKey, '1');
    }, 1200);
  }

  closeBtn.addEventListener('click', () => chat.classList.remove('show'));
})();
