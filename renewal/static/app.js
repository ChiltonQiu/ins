// Theme. The head script has already applied the stored choice; this only has
// to keep the buttons in step and write the change.
(function () {
  var buttons = document.querySelectorAll("[data-theme-set]");
  if (!buttons.length) return;

  function current() {
    return document.documentElement.dataset.theme || "system";
  }

  function paint() {
    var now = current();
    buttons.forEach(function (button) {
      button.setAttribute(
        "aria-pressed",
        button.dataset.themeSet === now ? "true" : "false"
      );
    });
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      var choice = button.dataset.themeSet;
      if (choice === "system") {
        delete document.documentElement.dataset.theme;
      } else {
        document.documentElement.dataset.theme = choice;
      }
      try {
        if (choice === "system") {
          localStorage.removeItem("renewal:theme");
        } else {
          localStorage.setItem("renewal:theme", choice);
        }
      } catch (e) {
        // Storage is blocked: the choice holds for this page only.
      }
      paint();
    });
  });

  paint();
})();

// Correcting a field is one click, typing, and Enter. No save button:
// friction here destroys the corrections dataset.
document.querySelectorAll("input.correctable").forEach(function (input) {
  var flag = document.querySelector(
    '[data-saved-for="' + input.dataset.fieldId + '"]'
  );
  var clearTimer;

  function report(state, text) {
    if (!flag) return;
    window.clearTimeout(clearTimer);
    flag.dataset.state = state;
    flag.textContent = text;
    // "saved" is confirmation, not a permanent label. Leaving it on every row
    // turns the whole column green and the next real save stops registering.
    if (state === "saved") {
      clearTimer = window.setTimeout(function () {
        flag.dataset.state = "";
      }, 2400);
    }
  }

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

    function rollback() {
      if (input.dataset.saved === attempted) {
        input.dataset.saved = previouslySaved;
      }
    }

    var body = new FormData();
    body.append("corrected_value", attempted);
    fetch("/fields/" + input.dataset.fieldId + "/correct", {
      method: "POST",
      body: body,
    })
      .then(function (response) {
        if (response.ok) {
          report("saved", "saved");
        } else {
          rollback();
          report("failed", "save failed");
        }
      })
      .catch(function () {
        rollback();
        report("failed", "offline");
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
// They do reload, and the field tables are long, so put the reader back where
// they were rather than at the top of the run.
var SCROLL_KEY = "renewal:scroll:" + window.location.pathname;

document.querySelectorAll("form.inline, form.add-missing").forEach(function (form) {
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var button = form.querySelector("button");
    var label = button ? button.textContent : null;
    if (button) {
      button.disabled = true;
    }
    sessionStorage.setItem(SCROLL_KEY, String(window.scrollY));
    fetch(form.action, { method: "POST", body: new FormData(form) })
      .then(function (response) {
        if (response.ok) {
          window.location.reload();
          return;
        }
        throw new Error(String(response.status));
      })
      .catch(function () {
        sessionStorage.removeItem(SCROLL_KEY);
        if (button) {
          button.disabled = false;
          button.textContent = label + " failed";
          window.setTimeout(function () {
            button.textContent = label;
          }, 2400);
        }
      });
  });
});

// Picking a class from the dropdown is the whole action; a second click on a
// "reclassify" button next to it was pure ceremony.
document.querySelectorAll("form.reclass select").forEach(function (select) {
  select.addEventListener("change", function () {
    select.form.requestSubmit();
  });
});

var restored = sessionStorage.getItem(SCROLL_KEY);
if (restored !== null) {
  sessionStorage.removeItem(SCROLL_KEY);
  window.scrollTo({ top: Number(restored), behavior: "instant" });
}
