from django.contrib import admin
from django.urls import include, path

from concerts import views as concerts_views

urlpatterns = [
    path('admin/', admin.site.urls),
    # Must match SPOTIFY_REDIRECT_URI registered in the Spotify developer dashboard.
    path('callback/', concerts_views.spotify_callback, name='spotify_callback'),
    path('', include('concerts.urls')),
]
