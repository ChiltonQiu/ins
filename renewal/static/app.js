// Correcting a field is one click, typing, and Enter. No save button:
// friction here destroys the corrections dataset.
document.querySelectorAll("input.correctable").forEach(function (input) {
  function save() {
    if (input.value === input.dataset.saved) return;
    if (input.value === input.dataset.original) return;
    // Enter triggers save() and then input.blur(), and blur() dispatches its
    // event synchronously — before this fetch's .then() runs. Mark the value
    // saved *before* issuing the request so that re-entrant call sees
    // input.value === input.dataset.saved and returns immediately, instead of
    // firing a second identical POST. Roll back on failure so a retry of the
    // same value is not silently swallowed by the dedupe guard above.
    var attempted = input.value;
    var previouslySaved = input.dataset.saved;
    input.dataset.saved = attempted;
    var body = new FormData();
    body.append("corrected_value", attempted);
    fetch("/fields/" + input.dataset.fieldId + "/correct", {
      method: "POST",
      body: body,
    }).then(function (response) {
      var flag = document.querySelector(
        '[data-saved-for="' + input.dataset.fieldId + '"]'
      );
      if (response.ok) {
        flag.textContent = "saved";
      } else {
        if (input.dataset.saved === attempted) {
          input.dataset.saved = previouslySaved;
        }
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
