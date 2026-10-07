const upload = document.querySelector('#datasets');
if (upload) upload.addEventListener('change', () => { if (upload.files.length) document.querySelector('#upload-form').submit(); });
document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
  const area = document.querySelector('textarea[name="question"]'); area.value = button.dataset.question; area.focus();
}));
const ask = document.querySelector('#ask-form');
if (ask) ask.addEventListener('submit', () => document.querySelector('#loading').classList.add('show'));
const copy = document.querySelector('#copy-code');
if (copy) copy.addEventListener('click', async () => { await navigator.clipboard.writeText(document.querySelector('#proof-code').innerText); copy.textContent = 'Copied ✓'; });
