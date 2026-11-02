(function recruiterCareersWidget() {
  "use strict";

  var script = document.currentScript;
  var defaultApiBase = script && script.src ? new URL(script.src).origin : "";
  var initialized = new WeakSet();
  var maxCandidateDocumentBytes = 15 * 1024 * 1024;
  var copy = {
    ru: {
      heading: "Открытые вакансии",
      intro: "Присоединяйтесь к нашей команде.",
      search: "Поиск по вакансиям",
      searchPlaceholder: "Название или описание…",
      loading: "Загружаем вакансии…",
      loadingMore: "Загружаем ещё…",
      empty: "Сейчас открытых вакансий нет.",
      noMatches: "По вашему запросу вакансий не найдено.",
      error: "Не удалось загрузить вакансии. Попробуйте позже.",
      apply: "Откликнуться",
      more: "Показать ещё",
      locationFallback: "Локация не указана",
      applyTitle: "Откликнуться на вакансию",
      applyIntro: "Оставьте контакты и прикрепите CV — заявка сразу попадёт рекрутеру.",
      close: "Закрыть",
      positionTag: "Открыта вакансия",
      name: "Имя",
      namePlaceholder: "Иван Иванов",
      phone: "Телефон",
      phonePlaceholder: "+992 90 123 4567",
      email: "Email",
      optional: "по желанию",
      emailPlaceholder: "ivan@example.com",
      resume: "Резюме",
      fileMode: "Файл",
      textMode: "Текст",
      chooseFile: "Загрузить CV",
      fileHint: "PDF или DOCX · до 15 МиБ",
      replaceFile: "Нажмите, чтобы заменить",
      textPlaceholder: "Вставьте резюме текстом: опыт, навыки и проекты.",
      consent: "Отправляя заявку, вы соглашаетесь на обработку данных рекрутером.",
      submit: "Отправить отклик",
      submitting: "Отправляем…",
      openSeparate: "Открыть отдельную форму",
      nameRequired: "Укажите ваше имя.",
      phoneRequired: "Укажите номер телефона.",
      resumeRequired: "Прикрепите CV или вставьте резюме текстом.",
      textTooShort: "Текст резюме должен содержать минимум 50 символов.",
      fileTooBig: "Файл больше 15 МиБ. Выберите файл меньшего размера.",
      fileType: "Поддерживаются только PDF и DOCX.",
      duplicateTitle: "Этот контакт уже есть в базе",
      duplicateHint: "Рекрутер уже получил ваши данные и свяжется с вами напрямую.",
      submitError: "Не удалось отправить отклик. Попробуйте ещё раз.",
      successTitle: "Отклик получен",
      successDesc: "Спасибо! Рекрутер посмотрит ваше CV и свяжется с вами, если профиль подходит.",
    },
    en: {
      heading: "Open positions",
      intro: "Join our team.",
      search: "Search jobs",
      searchPlaceholder: "Title or description…",
      loading: "Loading positions…",
      loadingMore: "Loading more…",
      empty: "There are no open positions right now.",
      noMatches: "No positions match your search.",
      error: "Positions couldn't be loaded. Please try again later.",
      apply: "Apply",
      more: "Show more",
      locationFallback: "Location not specified",
      applyTitle: "Apply for this position",
      applyIntro: "Share your contact details and CV — your application goes straight to the recruiter.",
      close: "Close",
      positionTag: "Now hiring",
      name: "Name",
      namePlaceholder: "Jane Doe",
      phone: "Phone",
      phonePlaceholder: "+992 90 123 4567",
      email: "Email",
      optional: "optional",
      emailPlaceholder: "jane@example.com",
      resume: "Resume",
      fileMode: "File",
      textMode: "Text",
      chooseFile: "Upload CV",
      fileHint: "PDF or DOCX · up to 15 MiB",
      replaceFile: "Click to replace",
      textPlaceholder: "Paste your resume as text: experience, skills, and projects.",
      consent: "By applying, you consent to your data being reviewed by the recruiter.",
      submit: "Submit application",
      submitting: "Submitting…",
      openSeparate: "Open separate form",
      nameRequired: "Please enter your name.",
      phoneRequired: "Please enter your phone number.",
      resumeRequired: "Attach a CV or paste your resume as text.",
      textTooShort: "Resume text must contain at least 50 characters.",
      fileTooBig: "The file is over 15 MiB. Choose a smaller file.",
      fileType: "Only PDF and DOCX files are supported.",
      duplicateTitle: "We already have this contact",
      duplicateHint: "The recruiter already has your details and will contact you directly.",
      submitError: "The application couldn't be submitted. Please try again.",
      successTitle: "Application received",
      successDesc: "Thank you! The recruiter will review your CV and contact you if your profile is a fit.",
    },
  };
  var enumLabels = {
    ru: {
      hybrid: "Гибрид",
      onsite: "В офисе",
      on_site: "В офисе",
      remote: "Удалённо",
      full_time: "Полная занятость",
      part_time: "Частичная занятость",
      contract: "Контракт",
      internship: "Стажировка",
      temporary: "Временная работа",
    },
    en: {
      hybrid: "Hybrid",
      onsite: "On-site",
      on_site: "On-site",
      remote: "Remote",
      full_time: "Full-time",
      part_time: "Part-time",
      contract: "Contract",
      internship: "Internship",
      temporary: "Temporary",
    },
  };

  function create(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function validAccent(value) {
    return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value)
      ? value
      : "#1867c0";
  }

  function initialize(host) {
    if (initialized.has(host)) return;
    initialized.add(host);

    var organization = (host.dataset.organization || "").trim();
    var apiBase = (host.dataset.apiBase || defaultApiBase).replace(/\/$/, "");
    var requestedLocale = (host.dataset.locale || document.documentElement.lang || "ru")
      .toLowerCase()
      .split("-")[0];
    var locale = requestedLocale === "en" ? "en" : "ru";
    var messages = copy[locale];
    var parsedLimit = Number.parseInt(host.dataset.limit || "12", 10);
    var limit = Number.isFinite(parsedLimit)
      ? Math.min(100, Math.max(1, parsedLimit))
      : 12;

    var root = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;
    var style = create("style");
    style.textContent = [
      ":host{--rc-accent:#1867c0;display:block;color:#17191c;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}",
      "*{box-sizing:border-box}",
      ".shell{width:100%}",
      ".header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:28px}",
      ".brand{display:flex;align-items:center;gap:12px;min-width:0}",
      ".logo{width:42px;height:42px;object-fit:contain;border-radius:10px}",
      ".heading{margin:0;font-size:clamp(28px,4vw,44px);line-height:1.08;letter-spacing:-.035em}",
      ".intro{margin:8px 0 0;color:#666d78;font-size:15px;line-height:1.5}",
      ".search-wrap{position:relative;width:min(100%,340px)}",
      ".search{width:100%;min-height:44px;border:1px solid #dfe3e8;border-radius:12px;background:#fff;color:#17191c;padding:10px 14px;font:inherit;font-size:14px;outline:none;transition:border-color .15s,box-shadow .15s}",
      ".search:focus{border-color:var(--rc-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--rc-accent) 15%,transparent)}",
      ".list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:16px}",
      ".job{display:flex;min-height:230px;flex-direction:column;border:1px solid #dfe3e8;border-radius:18px;background:#fff;padding:22px;box-shadow:0 1px 2px rgba(18,24,33,.03);transition:transform .15s,border-color .15s,box-shadow .15s}",
      ".job:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--rc-accent) 35%,#dfe3e8);box-shadow:0 12px 30px rgba(18,24,33,.08)}",
      ".title{margin:0 0 12px;font-size:20px;line-height:1.25;letter-spacing:-.02em}",
      ".meta{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:14px}",
      ".chip{border-radius:999px;background:#f2f4f6;color:#5e6570;padding:5px 9px;font-size:12px;line-height:1.2}",
      ".description{display:-webkit-box;overflow:hidden;margin:0 0 20px;color:#555d68;font-size:14px;line-height:1.55;-webkit-box-orient:vertical;-webkit-line-clamp:3}",
      ".apply{display:inline-flex;align-items:center;justify-content:center;align-self:flex-start;margin-top:auto;border:0;border-radius:10px;background:var(--rc-accent);color:#fff;padding:10px 15px;text-decoration:none;font:inherit;font-size:13px;font-weight:650;cursor:pointer;transition:filter .15s,transform .15s}",
      ".apply:hover{filter:brightness(.92)}.apply:active{transform:scale(.98)}",
      ".state{border:1px solid #e3e6ea;border-radius:14px;background:#f8f9fa;color:#656c76;padding:22px;text-align:center;font-size:14px}",
      ".state.error{border-color:#f2c8c8;background:#fff7f7;color:#a32d2d}",
      ".more{display:flex;min-width:150px;justify-content:center;margin:22px auto 0;border:1px solid #d9dde3;border-radius:10px;background:#fff;color:#23272d;padding:10px 16px;font:inherit;font-size:13px;font-weight:650;cursor:pointer;transition:border-color .15s,background .15s}",
      ".more:hover{border-color:#b9c0c9;background:#f7f8f9}.more:disabled{cursor:wait;opacity:.55}",
      ".modal{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:24px;background:rgba(16,20,26,.55);backdrop-filter:blur(5px)}",
      ".modal-panel{width:min(100%,620px);max-height:min(92vh,820px);overflow-y:auto;border:1px solid #dfe3e8;border-radius:20px;background:#fff;padding:24px;box-shadow:0 30px 80px rgba(9,14,21,.24)}",
      ".modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:8px}",
      ".modal-tag{margin:0 0 7px;color:#7a818c;font-size:10px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}",
      ".modal-title{margin:0;font-size:24px;line-height:1.18;letter-spacing:-.025em}",
      ".modal-close{display:flex;width:34px;height:34px;flex:0 0 auto;align-items:center;justify-content:center;border:1px solid #e0e3e7;border-radius:9px;background:#fff;color:#626a75;font:inherit;font-size:20px;cursor:pointer}",
      ".modal-close:hover{background:#f5f6f7;color:#17191c}",
      ".modal-intro{margin:0 0 22px;color:#666d78;font-size:13px;line-height:1.55}",
      ".form{display:flex;flex-direction:column;gap:16px}",
      ".form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}",
      ".field{display:flex;flex-direction:column;gap:7px}",
      ".field-label{color:#717984;font-size:10px;font-weight:700;letter-spacing:.075em;text-transform:uppercase}",
      ".required{color:#b93232}.optional{font-weight:500;letter-spacing:0;text-transform:none}",
      ".input,.textarea{width:100%;border:1px solid #dfe3e8;border-radius:9px;background:#fff;color:#17191c;padding:10px 12px;font:inherit;font-size:13px;outline:none;transition:border-color .15s,box-shadow .15s}",
      ".input:focus,.textarea:focus{border-color:var(--rc-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--rc-accent) 14%,transparent)}",
      ".textarea{min-height:150px;resize:vertical;line-height:1.5}",
      ".resume-head{display:flex;align-items:center;gap:10px}",
      ".modes{display:flex;align-items:center;gap:4px;border-radius:8px;background:#f1f3f5;padding:3px}",
      ".mode{border:0;border-radius:6px;background:transparent;color:#6b727d;padding:5px 9px;font:inherit;font-size:11px;cursor:pointer}",
      ".mode.active{background:#fff;color:#17191c;box-shadow:0 1px 3px rgba(18,24,33,.1);font-weight:650}",
      ".upload{display:flex;width:100%;align-items:center;justify-content:center;border:1px dashed #cfd4da;border-radius:11px;background:#fafbfc;padding:20px;font:inherit;cursor:pointer;transition:border-color .15s,background .15s}",
      ".upload:hover{border-color:var(--rc-accent);background:color-mix(in srgb,var(--rc-accent) 4%,#fff)}",
      ".upload-copy{display:flex;flex-direction:column;gap:4px;text-align:center}.upload-title{font-size:13px;font-weight:650}.upload-hint{color:#747b85;font-size:11px}",
      ".form-error{border:1px solid #efc3c3;border-radius:9px;background:#fff6f6;color:#a42f2f;padding:11px 12px;font-size:12px;line-height:1.5}",
      ".form-error strong{display:block;margin-bottom:2px}",
      ".form-actions{display:flex;align-items:center;justify-content:space-between;gap:16px;padding-top:2px}",
      ".consent{max-width:340px;color:#7a818b;font-size:10px;line-height:1.45}",
      ".submit{flex:0 0 auto;border:0;border-radius:9px;background:var(--rc-accent);color:#fff;padding:11px 16px;font:inherit;font-size:13px;font-weight:650;cursor:pointer}",
      ".submit:disabled{cursor:wait;opacity:.55}",
      ".fallback{align-self:center;color:#6b727c;font-size:11px;text-decoration:underline;text-underline-offset:3px}",
      ".success{display:flex;flex-direction:column;align-items:center;border:1px solid color-mix(in srgb,var(--rc-accent) 24%,#dfe3e8);border-radius:14px;background:color-mix(in srgb,var(--rc-accent) 6%,#fff);padding:30px 22px;text-align:center}",
      ".success-mark{display:flex;width:44px;height:44px;align-items:center;justify-content:center;border-radius:50%;background:var(--rc-accent);color:#fff;font-size:22px;font-weight:700}",
      ".success-title{margin:14px 0 5px;font-size:17px}.success-desc{max-width:410px;margin:0;color:#626a74;font-size:13px;line-height:1.55}",
      "[hidden]{display:none!important}",
      "@media(max-width:720px){.header{flex-direction:column}.search-wrap{width:100%}.job{min-height:0}.modal{align-items:flex-end;padding:0}.modal-panel{max-height:94vh;border-radius:20px 20px 0 0;padding:20px}.form-row{grid-template-columns:1fr}.form-actions{align-items:stretch;flex-direction:column}.consent{max-width:none}.submit{width:100%}}",
      "@media(prefers-reduced-motion:reduce){.job,.apply,.search,.more{transition:none}}",
    ].join("");
    root.appendChild(style);

    var shell = create("section", "shell");
    shell.setAttribute("aria-label", messages.heading);
    var header = create("header", "header");
    var brand = create("div", "brand");
    var brandText = create("div");
    var heading = create(
      "h2",
      "heading",
      host.dataset.title || messages.heading,
    );
    var intro = create("p", "intro", host.dataset.intro || messages.intro);
    brandText.appendChild(heading);
    brandText.appendChild(intro);
    brand.appendChild(brandText);
    header.appendChild(brand);

    var searchWrap = create("label", "search-wrap");
    var search = create("input", "search");
    search.type = "search";
    search.placeholder = messages.searchPlaceholder;
    search.setAttribute("aria-label", messages.search);
    searchWrap.appendChild(search);
    header.appendChild(searchWrap);
    shell.appendChild(header);

    var stateNode = create("div", "state", messages.loading);
    stateNode.setAttribute("role", "status");
    var list = create("div", "list");
    list.setAttribute("aria-live", "polite");
    var more = create("button", "more", messages.more);
    more.type = "button";
    more.hidden = true;
    shell.appendChild(stateNode);
    shell.appendChild(list);
    shell.appendChild(more);
    root.appendChild(shell);

    if (!apiBase || !organization) {
      stateNode.classList.add("error");
      stateNode.textContent = messages.error;
      return;
    }

    var nextCursor = null;
    var controller = null;
    var searchTimer = null;
    var hasBrand = false;
    var modal = create("div", "modal");
    var opener = null;
    modal.hidden = true;
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    root.appendChild(modal);

    function createField(labelText, input, required, optional) {
      var field = create("label", "field");
      var label = create("span", "field-label", labelText);
      if (required) label.appendChild(create("span", "required", " *"));
      if (optional) {
        label.appendChild(create("span", "optional", " · " + messages.optional));
      }
      field.appendChild(label);
      field.appendChild(input);
      return field;
    }

    function closeApplication() {
      modal.hidden = true;
      modal.replaceChildren();
      document.removeEventListener("keydown", onModalKeydown);
      if (opener && typeof opener.focus === "function") opener.focus();
      opener = null;
    }

    function onModalKeydown(event) {
      if (event.key === "Escape") closeApplication();
    }

    modal.addEventListener("click", function closeOnBackdrop(event) {
      if (event.target === modal) closeApplication();
    });

    function openApplication(job, trigger) {
      opener = trigger;
      modal.replaceChildren();
      modal.hidden = false;
      document.addEventListener("keydown", onModalKeydown);

      var panel = create("section", "modal-panel");
      var head = create("div", "modal-head");
      var titleWrap = create("div");
      titleWrap.appendChild(create("p", "modal-tag", messages.positionTag));
      var modalTitle = create("h3", "modal-title", job.title);
      titleWrap.appendChild(modalTitle);
      var close = create("button", "modal-close", "×");
      close.type = "button";
      close.setAttribute("aria-label", messages.close);
      close.addEventListener("click", closeApplication);
      head.appendChild(titleWrap);
      head.appendChild(close);
      panel.appendChild(head);
      panel.appendChild(create("p", "modal-intro", messages.applyIntro));

      var form = create("form", "form");
      var name = create("input", "input");
      name.name = "name";
      name.autocomplete = "name";
      name.placeholder = messages.namePlaceholder;
      name.maxLength = 200;
      var phone = create("input", "input");
      phone.name = "phone";
      phone.type = "tel";
      phone.inputMode = "tel";
      phone.autocomplete = "tel";
      phone.placeholder = messages.phonePlaceholder;
      phone.value = host.dataset.phonePrefix || "+992 ";
      phone.maxLength = 64;
      var email = create("input", "input");
      email.name = "email";
      email.type = "email";
      email.inputMode = "email";
      email.autocomplete = "email";
      email.placeholder = messages.emailPlaceholder;
      email.maxLength = 320;

      form.appendChild(createField(messages.name, name, true, false));
      var contactRow = create("div", "form-row");
      contactRow.appendChild(createField(messages.phone, phone, true, false));
      contactRow.appendChild(createField(messages.email, email, false, true));
      form.appendChild(contactRow);

      var resumeField = create("div", "field");
      var resumeHead = create("div", "resume-head");
      var resumeLabel = create("span", "field-label", messages.resume);
      resumeLabel.appendChild(create("span", "required", " *"));
      var modes = create("div", "modes");
      var fileMode = create("button", "mode active", messages.fileMode);
      var textMode = create("button", "mode", messages.textMode);
      fileMode.type = "button";
      textMode.type = "button";
      modes.appendChild(fileMode);
      modes.appendChild(textMode);
      resumeHead.appendChild(resumeLabel);
      resumeHead.appendChild(modes);
      resumeField.appendChild(resumeHead);

      var fileInput = create("input");
      fileInput.type = "file";
      fileInput.accept = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
      fileInput.hidden = true;
      var upload = create("button", "upload");
      upload.type = "button";
      var uploadCopy = create("span", "upload-copy");
      var uploadTitle = create("span", "upload-title", messages.chooseFile);
      var uploadHint = create("span", "upload-hint", messages.fileHint);
      uploadCopy.appendChild(uploadTitle);
      uploadCopy.appendChild(uploadHint);
      upload.appendChild(uploadCopy);
      var textResume = create("textarea", "textarea");
      textResume.placeholder = messages.textPlaceholder;
      textResume.maxLength = 200000;
      textResume.hidden = true;
      resumeField.appendChild(fileInput);
      resumeField.appendChild(upload);
      resumeField.appendChild(textResume);
      form.appendChild(resumeField);

      var error = create("div", "form-error");
      error.setAttribute("role", "alert");
      error.hidden = true;
      form.appendChild(error);

      var actions = create("div", "form-actions");
      actions.appendChild(create("span", "consent", messages.consent));
      var submit = create("button", "submit", messages.submit);
      submit.type = "submit";
      actions.appendChild(submit);
      form.appendChild(actions);
      panel.appendChild(form);

      var fallback = create("a", "fallback", messages.openSeparate);
      fallback.href = job.apply_url;
      fallback.target = "_blank";
      fallback.rel = "noreferrer";
      panel.appendChild(fallback);
      modal.appendChild(panel);

      var mode = "file";
      var selectedFile = null;

      function showError(title, hint) {
        error.replaceChildren();
        if (title) error.appendChild(create("strong", "", title));
        if (hint) error.appendChild(create("span", "", hint));
        error.hidden = false;
      }

      function clearError() {
        error.hidden = true;
        error.replaceChildren();
      }

      function setMode(nextMode) {
        mode = nextMode;
        fileMode.classList.toggle("active", mode === "file");
        textMode.classList.toggle("active", mode === "text");
        upload.hidden = mode !== "file";
        textResume.hidden = mode !== "text";
        clearError();
      }

      fileMode.addEventListener("click", function selectFileMode() {
        setMode("file");
      });
      textMode.addEventListener("click", function selectTextMode() {
        setMode("text");
      });
      upload.addEventListener("click", function chooseResume() {
        fileInput.click();
      });
      fileInput.addEventListener("change", function selectResume() {
        var nextFile = fileInput.files && fileInput.files[0];
        if (!nextFile) return;
        var lowerName = nextFile.name.toLowerCase();
        if (!lowerName.endsWith(".pdf") && !lowerName.endsWith(".docx")) {
          fileInput.value = "";
          selectedFile = null;
          showError("", messages.fileType);
          return;
        }
        if (nextFile.size > maxCandidateDocumentBytes) {
          fileInput.value = "";
          selectedFile = null;
          showError("", messages.fileTooBig);
          return;
        }
        selectedFile = nextFile;
        uploadTitle.textContent = nextFile.name;
        uploadHint.textContent = messages.replaceFile;
        clearError();
      });

      form.addEventListener("submit", async function submitApplication(event) {
        event.preventDefault();
        clearError();
        if (!name.value.trim()) {
          showError("", messages.nameRequired);
          name.focus();
          return;
        }
        if (phone.value.trim().length < 4) {
          showError("", messages.phoneRequired);
          phone.focus();
          return;
        }
        if (!email.checkValidity()) {
          email.reportValidity();
          return;
        }
        if (mode === "file" && !selectedFile) {
          showError("", messages.resumeRequired);
          return;
        }
        if (mode === "text" && textResume.value.trim().length < 50) {
          showError("", messages.textTooShort);
          textResume.focus();
          return;
        }

        var body = new FormData();
        body.set("name", name.value.trim());
        body.set("phone", phone.value.trim());
        if (email.value.trim()) body.set("email", email.value.trim());
        body.set("source", (host.dataset.source || "company_website").slice(0, 64));
        if (mode === "file") body.set("file", selectedFile);
        if (mode === "text") body.set("raw_text", textResume.value.trim());

        submit.disabled = true;
        submit.textContent = messages.submitting;
        var endpoint = new URL(
          "/public/v1/careers/" +
            encodeURIComponent(organization) +
            "/positions/" +
            encodeURIComponent(job.id) +
            "/apply",
          apiBase,
        );
        try {
          var response = await fetch(endpoint.toString(), {
            method: "POST",
            body: body,
            headers: { Accept: "application/json" },
          });
          if (response.status === 409) {
            showError(messages.duplicateTitle, messages.duplicateHint);
            return;
          }
          if (!response.ok) throw new Error("Application API returned " + response.status);

          var success = create("div", "success");
          success.appendChild(create("span", "success-mark", "✓"));
          success.appendChild(create("h4", "success-title", messages.successTitle));
          success.appendChild(create("p", "success-desc", messages.successDesc));
          form.replaceChildren(success);
          fallback.hidden = true;
        } catch (requestError) {
          showError("", messages.submitError);
          if (window.console) console.error("Recruiter application widget", requestError);
        } finally {
          submit.disabled = false;
          submit.textContent = messages.submit;
        }
      });

      window.setTimeout(function focusApplication() {
        name.focus();
      }, 0);
    }

    function updateBrand(organizationData) {
      var themeHost = root.host || host;
      themeHost.style.setProperty(
        "--rc-accent",
        validAccent(organizationData.accent_color),
      );
      if (hasBrand || !organizationData.logo_url) return;
      var logo = create("img", "logo");
      logo.src = organizationData.logo_url;
      logo.alt = organizationData.name || "";
      logo.loading = "lazy";
      logo.addEventListener(
        "error",
        function hideUnavailableLogo() {
          logo.hidden = true;
        },
        { once: true },
      );
      brand.insertBefore(logo, brandText);
      hasBrand = true;
    }

    function renderJob(job) {
      var article = create("article", "job");
      article.appendChild(create("h3", "title", job.title));

      var meta = create("div", "meta");
      [
        job.location || messages.locationFallback,
        enumLabels[locale][String(job.work_format || "").toLowerCase()] ||
          job.work_format,
        enumLabels[locale][String(job.employment_type || "").toLowerCase()] ||
          job.employment_type,
        job.salary_range,
      ].forEach(function addMeta(value) {
        if (value) meta.appendChild(create("span", "chip", value));
      });
      article.appendChild(meta);

      if (job.description) {
        article.appendChild(create("p", "description", job.description));
      }
      var apply = create("button", "apply", messages.apply);
      apply.type = "button";
      apply.addEventListener("click", function showApplication() {
        openApplication(job, apply);
      });
      article.appendChild(apply);
      list.appendChild(article);
    }

    async function load(reset) {
      if (controller) controller.abort();
      controller = new AbortController();
      more.disabled = true;
      if (reset) {
        nextCursor = null;
        list.replaceChildren();
        stateNode.hidden = false;
        stateNode.classList.remove("error");
        stateNode.textContent = messages.loading;
      } else {
        more.textContent = messages.loadingMore;
      }

      var url = new URL(
        "/public/v1/careers/" + encodeURIComponent(organization) + "/positions",
        apiBase,
      );
      url.searchParams.set("limit", String(limit));
      if (!reset && nextCursor) url.searchParams.set("cursor", nextCursor);
      if (search.value.trim()) url.searchParams.set("q", search.value.trim());

      try {
        var response = await fetch(url.toString(), {
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Careers API returned " + response.status);
        var page = await response.json();
        updateBrand(page.organization);
        page.items.forEach(renderJob);
        nextCursor = page.next_cursor;
        more.hidden = !nextCursor;
        stateNode.hidden = list.childElementCount > 0;
        if (page.total === 0) {
          stateNode.textContent = search.value.trim()
            ? messages.noMatches
            : messages.empty;
        }
      } catch (error) {
        if (error && error.name === "AbortError") return;
        stateNode.hidden = false;
        stateNode.classList.add("error");
        stateNode.textContent = messages.error;
        more.hidden = true;
        if (window.console) console.error("Recruiter careers widget", error);
      } finally {
        more.disabled = false;
        more.textContent = messages.more;
      }
    }

    search.addEventListener("input", function onSearch() {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(function runSearch() {
        load(true);
      }, 300);
    });
    more.addEventListener("click", function loadMore() {
      load(false);
    });
    load(true);
  }

  function boot() {
    document
      .querySelectorAll("[data-recruiter-careers]")
      .forEach(initialize);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
