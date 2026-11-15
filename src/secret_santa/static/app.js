// Progressive enhancement only: every form works without this file.

// Ask before destructive actions, then stop double submits (a double-clicked "Losuj i wyślij"
// would otherwise send everything twice).
document.addEventListener("submit", (event) => {
  const form = event.target;
  const question = form.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
    return;
  }
  for (const button of form.querySelectorAll("button[type=submit]")) {
    button.disabled = true;
    button.dataset.label = button.textContent;
    button.textContent = "Chwileczkę…";
  }
});

// Restore buttons when the page comes back from the bfcache (browser "back").
window.addEventListener("pageshow", () => {
  for (const button of document.querySelectorAll("button[data-label]")) {
    button.disabled = false;
    button.textContent = button.dataset.label;
  }
});

// "Kopiuj" next to the share link.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const input = document.querySelector(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.select();
    document.execCommand("copy");
  }
  const label = button.textContent;
  button.textContent = "Skopiowano!";
  setTimeout(() => (button.textContent = label), 1500);
});
