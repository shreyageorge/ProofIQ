const DB_NAME = 'proofiq-workspace';
const STORE_NAME = 'datasets';

function openDatasetStore() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE_NAME, { keyPath: 'id' });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function saveDatasets(files) {
  const db = await openDatasetStore();
  await new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    const store = transaction.objectStore(STORE_NAME);
    store.clear();
    Array.from(files).forEach(file => store.put({
      id: `${file.name}:${file.size}:${file.lastModified}`,
      file,
    }));
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
}

async function getDatasets() {
  const db = await openDatasetStore();
  const datasets = await new Promise((resolve, reject) => {
    const request = db.transaction(STORE_NAME).objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  db.close();
  return datasets;
}

async function clearDatasets() {
  const db = await openDatasetStore();
  await new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    transaction.objectStore(STORE_NAME).clear();
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
}

async function removeDatasetsByName(filename) {
  const db = await openDatasetStore();
  const datasets = await new Promise((resolve, reject) => {
    const request = db.transaction(STORE_NAME).objectStore(STORE_NAME).getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  await new Promise((resolve, reject) => {
    const transaction = db.transaction(STORE_NAME, 'readwrite');
    const store = transaction.objectStore(STORE_NAME);
    datasets.filter(dataset => dataset.file.name === filename).forEach(dataset => store.delete(dataset.id));
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(transaction.error);
  });
  db.close();
}

function bindPageControls() {
  const upload = document.querySelector('#datasets');
  if (upload) upload.addEventListener('change', async () => {
    if (!upload.files.length) return;
    await saveDatasets(upload.files);
    document.querySelector('#upload-form').submit();
  });

  document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click', () => {
    const area = document.querySelector('textarea[name="question"]');
    if (!area) return;
    area.value = button.dataset.question;
    area.dispatchEvent(new Event('input', { bubbles: true }));
    area.focus();
  }));

  const ask = document.querySelector('#ask-form');
  if (ask) ask.addEventListener('submit', async event => {
    event.preventDefault();
    document.querySelector('#loading').classList.add('show');
    try {
      const formData = new FormData(ask);
      const datasets = await getDatasets();
      datasets.forEach(dataset => formData.append('datasets', dataset.file, dataset.file.name));
      const response = await fetch(ask.action, {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': formData.get('csrfmiddlewaretoken') },
      });
      if (!response.ok) throw new Error(`Analysis request failed (${response.status}).`);
      const nextDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
      document.title = nextDocument.title;
      document.body.replaceWith(nextDocument.body);
      window.history.pushState({}, '', response.url);
      bindPageControls();
    } catch (error) {
      document.querySelector('#loading')?.classList.remove('show');
      window.alert(error.message);
    }
  });

  const clearForm = document.querySelector('#clear-form');
  if (clearForm) clearForm.addEventListener('submit', async event => {
    event.preventDefault();
    await clearDatasets();
    clearForm.submit();
  });

  document.querySelectorAll('.remove-file-form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    await removeDatasetsByName(form.dataset.filename);
    form.submit();
  }));

  const copy = document.querySelector('#copy-code');
  if (copy) copy.addEventListener('click', async () => {
    await navigator.clipboard.writeText(document.querySelector('#proof-code').innerText);
    copy.textContent = 'Copied ✓';
  });
}

bindPageControls();
