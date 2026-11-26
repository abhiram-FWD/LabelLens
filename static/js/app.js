(() => {
  const inputs = Array.from(document.querySelectorAll(".slot-input"));
  const previewList = document.getElementById("uploadPreviews");
  const errorBox = document.getElementById("uploadErrors");
  const uploadForm = document.getElementById("uploadForm");
  const overlay = document.getElementById("processingOverlay");
  const maxBytes = 5 * 1024 * 1024;

  if (inputs.length && previewList && uploadForm) {
    const showErrors = messages => {
      if (!errorBox) return;
      errorBox.replaceChildren();
      errorBox.hidden = messages.length === 0;
      if (!messages.length) return;
      const list = document.createElement("ul");
      list.className = "mb-0 ps-3";
      messages.forEach(message => {
        const item = document.createElement("li");
        item.textContent = message;
        list.append(item);
      });
      errorBox.append(list);
    };
    const getFiles = () => inputs.flatMap(input => Array.from(input.files || []).map(file => ({file, input})));
    const validate = entries => {
      const errors = [];
      if (!entries.length) errors.push("Choose at least one label photo to continue.");
      entries.forEach(({file}) => {
        if (!new Set(["image/jpeg", "image/png", "image/webp"]).has(file.type)) errors.push(`${file.name} must be a JPEG, PNG, or WEBP image.`);
        if (file.size > maxBytes) errors.push(`${file.name} is larger than 5 MB.`);
      });
      if (entries.length > 6) errors.push("Upload no more than 6 photos per scan.");
      return errors;
    };
    const setProgressStage = stage => {
      const nodes = Array.from(document.querySelectorAll("[data-upload-stage]"));
      const order = ["uploading", "reading", "checking", "completed"];
      const activeIndex = order.indexOf(stage);
      nodes.forEach(node => {
        const index = order.indexOf(node.dataset.uploadStage);
        node.classList.toggle("is-complete", index < activeIndex || stage === "completed" || (stage === "processing" && node.dataset.uploadStage === "uploading"));
        node.classList.toggle("is-current", index === activeIndex);
        node.classList.toggle("is-processing", stage === "processing" && (node.dataset.uploadStage === "reading" || node.dataset.uploadStage === "checking"));
        if (index === activeIndex) node.setAttribute("aria-current", "step");
        else node.removeAttribute("aria-current");
      });
      const label = document.getElementById("uploadProgressLabel");
      const note = document.getElementById("uploadStageNote");
      if (label) label.textContent = stage === "processing" ? "Reading text and checking rules" : ({uploading:"Uploading", reading:"Reading text", checking:"Checking rules", completed:"Completed"}[stage] || "Uploading");
      if (note) note.textContent = stage === "processing" ? "The server is processing your images. Results will appear when the checks finish." : ({uploading:"Sending your images securely.", completed:"Your scan is ready."}[stage] || "");
    };
    const renderFiles = () => {
      previewList.querySelectorAll("img[data-object-url]").forEach(img => URL.revokeObjectURL(img.src));
      previewList.replaceChildren();
      getFiles().forEach(({file, input}, globalIndex) => {
        const card = document.createElement("div");
        card.className = "preview-card";
        const img = document.createElement("img");
        img.alt = `${input.dataset.slot} label preview`;
        img.dataset.objectUrl = "true";
        img.src = URL.createObjectURL(file);
        const meta = document.createElement("div");
        meta.className = "preview-name";
        meta.textContent = `${input.dataset.slot} · ${file.name}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "preview-remove";
        remove.setAttribute("aria-label", `Remove ${file.name}`);
        remove.innerHTML = '<i class="bi bi-x-lg"></i>';
        remove.addEventListener("click", () => {
          const localIndex = getFiles().slice(0, globalIndex).filter(entry => entry.input === input).length;
          const transfer = new DataTransfer();
          Array.from(input.files).forEach((selected, index) => { if (index !== localIndex) transfer.items.add(selected); });
          input.files = transfer.files;
          renderFiles();
          showErrors([]);
        });
        card.append(img, meta, remove);
        previewList.append(card);
      });
      previewList.hidden = !getFiles().length;
      const count = document.getElementById("selectedCount");
      const number = getFiles().length;
      if (count) count.textContent = number ? `${number} photo${number === 1 ? "" : "s"} selected` : "No images selected yet";
    };
    const setInputFiles = (input, files) => {
      const transfer = new DataTransfer();
      Array.from(files).forEach(file => transfer.items.add(file));
      input.files = transfer.files;
      renderFiles();
      showErrors(validate(getFiles()));
    };
    inputs.forEach(input => {
      input.addEventListener("change", () => { renderFiles(); showErrors(validate(getFiles())); });
      const slot = input.closest(".upload-slot");
      if (!slot) return;
      ["dragenter", "dragover"].forEach(name => slot.addEventListener(name, event => {
        event.preventDefault();
        slot.classList.add("is-dragging");
      }));
      ["dragleave", "drop"].forEach(name => slot.addEventListener(name, event => {
        event.preventDefault();
        slot.classList.remove("is-dragging");
      }));
      slot.addEventListener("drop", event => {
        const dropped = Array.from(event.dataTransfer.files);
        if (input.multiple) setInputFiles(input, [...Array.from(input.files), ...dropped]);
        else setInputFiles(input, dropped.slice(0, 1));
      });
    });

    uploadForm.addEventListener("submit", event => {
      const entries = getFiles();
      const errors = validate(entries);
      showErrors(errors);
      if (errors.length) { event.preventDefault(); return; }
      if (uploadForm.dataset.submitting === "true") { event.preventDefault(); return; }
      event.preventDefault();
      uploadForm.dataset.submitting = "true";
      const button = uploadForm.querySelector("button[type='submit']");
      const progressWrap = document.getElementById("uploadProgressWrap");
      const progressBar = document.getElementById("uploadProgressBar");
      const progressValue = document.getElementById("uploadProgressValue");
      const progressLabel = document.getElementById("uploadProgressLabel");
      if (progressWrap) progressWrap.hidden = false;
      setProgressStage("uploading");
      if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Uploading…'; }
      const xhr = new XMLHttpRequest();
      xhr.open("POST", uploadForm.action || window.location.href);
      xhr.upload.addEventListener("progress", progress => {
        if (!progress.lengthComputable) return;
        const percent = Math.round(progress.loaded * 100 / progress.total);
        if (progressBar) progressBar.style.width = `${percent}%`;
        if (progressValue) progressValue.textContent = `${percent}%`;
        if (percent >= 100 && progressLabel) progressLabel.textContent = "Upload complete";
      });
      xhr.upload.addEventListener("load", () => {
        setProgressStage("processing");
        if (overlay) overlay.hidden = false;
      });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 400 && xhr.responseURL && !xhr.responseURL.includes("/upload/")) {
          setProgressStage("completed");
          if (progressBar) { progressBar.style.width = "100%"; progressBar.classList.remove("progress-bar-animated"); }
          if (progressValue) progressValue.textContent = "100%";
          if (overlay) overlay.hidden = false;
          window.location.assign(xhr.responseURL);
          return;
        }
        if (overlay) overlay.hidden = true;
        uploadForm.dataset.submitting = "false";
        if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-stars me-2"></i>Check this label'; }
        let messages = [];
        try {
          const responseDocument = new DOMParser().parseFromString(xhr.responseText, "text/html");
          messages = Array.from(responseDocument.querySelectorAll(".upload-server-errors li")).map(item => item.textContent.trim()).filter(Boolean);
          const singleError = responseDocument.querySelector(".upload-server-errors");
          if (!messages.length && singleError) messages = [singleError.textContent.trim()];
        } catch (_) { /* Use the friendly fallback below. */ }
        showErrors(messages.length ? messages : ["The image was uploaded, but the scan could not be completed. Please retry or choose a clearer image."]);
      });
      xhr.addEventListener("error", () => {
        if (overlay) overlay.hidden = true;
        uploadForm.dataset.submitting = "false";
        if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-stars me-2"></i>Check this label'; }
        showErrors(["The upload was interrupted. Check your connection and try again."]);
      });
      xhr.send(new FormData(uploadForm));
    });
  }

  const lightbox = document.getElementById("imageLightbox");
  const lightboxImage = document.getElementById("lightboxImage");
  if (lightbox && lightboxImage && window.bootstrap) {
    document.querySelectorAll("[data-lightbox-src]").forEach(button => button.addEventListener("click", () => {
      lightboxImage.src = button.dataset.lightboxSrc;
      lightboxImage.alt = button.dataset.lightboxAlt || "Enlarged product label";
      bootstrap.Modal.getOrCreateInstance(lightbox).show();
    }));
    lightbox.addEventListener("hidden.bs.modal", () => { lightboxImage.removeAttribute("src"); });
  }
})();
