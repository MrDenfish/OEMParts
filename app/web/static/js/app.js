/* OEMPartsAgent — HTMX config and helpers */

/* Loading indicator: dim the target during HTMX requests */
document.addEventListener("htmx:beforeRequest", function(event) {
    var target = event.detail.target;
    if (target) {
        target.classList.add("htmx-loading");
    }
});

document.addEventListener("htmx:afterRequest", function(event) {
    var target = event.detail.target;
    if (target) {
        target.classList.remove("htmx-loading");
    }
});

/* Log HTMX errors to console for debugging */
document.addEventListener("htmx:responseError", function(event) {
    console.error("HTMX request failed:", event.detail.xhr.status, event.detail.xhr.statusText);
});

/* Auto-dismiss flash messages after 5 seconds */
document.addEventListener("DOMContentLoaded", function() {
    var flashes = document.querySelectorAll(".flash");
    flashes.forEach(function(el) {
        setTimeout(function() {
            el.style.opacity = "0";
            el.style.transition = "opacity 0.3s";
            setTimeout(function() { el.remove(); }, 300);
        }, 5000);
    });
});
