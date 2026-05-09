/**
 * Error Debugging for Django Admin
 * Logs backend errors stored in cookies to the browser console.
 */
(function() {
    function getCookie(name) {
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    function deleteCookie(name) {
        document.cookie = name + '=; Path=/; Expires=Thu, 01 Jan 1970 00:00:01 GMT;';
    }

    function checkBackendErrors() {
        const errorTraceback = getCookie('django_error_traceback');
        if (errorTraceback) {
            console.group('%c Django Backend Error ', 'background: #f00; color: #fff; font-weight: bold; padding: 2px 5px; border-radius: 3px;');
            console.error(errorTraceback);
            console.groupEnd();
            deleteCookie('django_error_traceback');
        }
    }

    // Run on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', checkBackendErrors);
    } else {
        checkBackendErrors();
    }

    // Also check periodically for AJAX errors that might have set the cookie
    setInterval(checkBackendErrors, 2000);
})();
