// No inline scripts: behaviours are attached here via data-* attributes so that the
// Content-Security-Policy can work without 'unsafe-inline'.

// data-confirm: ask before acting (e.g. deleting a user)
document.addEventListener("click", function (e) {
  const button = e.target.closest("[data-confirm]");
  if (button && !window.confirm(button.dataset.confirm)) e.preventDefault();
});

// data-back: go back in history when there is any, otherwise follow the href
document.addEventListener("click", function (e) {
  const link = e.target.closest("[data-back]");
  if (link && window.history.length > 1) {
    e.preventDefault();
    window.history.back();
  }
});
