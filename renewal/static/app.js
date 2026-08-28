// Correcting a field is one click, typing, and Enter. No save button:
// friction here destroys the corrections dataset.
document.querySelectorAll("input.correctable").forEach(function (input) {
  function save() {
    if (input.value === input.dataset.saved) return;
    if (input.value === input.dataset.original) return;
    var body = new FormData();
    body.append("corrected_value", input.value);
    fetch("/fields/" + input.dataset.fieldId + "/correct", {
      method: "POST",
      body: body,
    }).then(function (response) {
      var flag = document.querySelector(
        '[data-saved-for="' + input.dataset.fieldId + '"]'
      );
      if (response.ok) {
        input.dataset.saved = input.value;
        flag.textContent = "saved";
      } else {
        flag.textContent = "save failed";
      }
    });
  }
  input.addEventListener("blur", save);
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      save();
      input.blur();
    }
  });
});

// The add-missing and reject forms post normally but must not navigate away.
document.querySelectorAll("form.inline, form.add-missing").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    fetch(form.action, { method: "POST", body: new FormData(form) }).then(function () {
      window.location.reload();
    });
  });
});
