(function () {
  const chat = document.getElementById('floating-chat');
  const closeBtn = document.getElementById('floating-chat-close');
  if (!chat || !closeBtn) return;

  if (!sessionStorage.getItem('chat_shown')) {
    setTimeout(() => {
      chat.classList.add('show');
      sessionStorage.setItem('chat_shown', '1');
    }, 45000);
  }

  closeBtn.addEventListener('click', () => chat.classList.remove('show'));
})();
