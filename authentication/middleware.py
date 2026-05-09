import traceback
import urllib.parse

class AdminErrorConsoleMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        
        # If an error was captured during the request, set it in a cookie
        if hasattr(request, '_django_error_tb') and request.path.startswith('/admin/'):
            # We use urllib.parse.quote to ensure the traceback is safe for a cookie
            tb_quoted = urllib.parse.quote(request._django_error_tb)
            # Set the cookie on the response
            response.set_cookie('django_error_traceback', tb_quoted, max_age=30, samesite='Lax')
            
        return response

    def process_exception(self, request, exception):
        """
        Capture the exception and store it on the request object.
        Django's exception handling will continue after this.
        """
        if request.path.startswith('/admin/'):
            request._django_error_tb = traceback.format_exc()
        return None

class AuthenticationBypassMiddleware:
    
    def __init__(self, get_response):
        self.get_response = get_response
        self.public_paths = [
            '/api/auth/register/',
            '/api/auth/verify-email/',
            '/api/auth/login/',
            '/api/auth/resend-verification/',
        ]

    def __call__(self, request):
        if any(request.path.startswith(path) for path in self.public_paths):
            request.META['BYPASS_AUTH'] = True
        
        return self.get_response(request)
