from django.urls import path

from . import views

app_name = 'concerts'

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', views.spotify_login, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('artists/', views.favorite_artists, name='favorite_artists'),
    path('followed/', views.followed_artists, name='followed_artists'),
    path('followed/<str:artist_id>/unfollow/', views.unfollow_artist, name='unfollow_artist'),
    path('followed/<str:artist_id>/follow/', views.follow_artist, name='follow_artist'),
    path('on-tour/', views.on_tour, name='on_tour'),
    path('on-tour/progress/<str:job_id>/', views.on_tour_progress, name='on_tour_progress'),
    path('artist/<str:attraction_id>/', views.artist_schedule, name='artist_schedule'),
    path('search/', views.search_by_location, name='search'),
    path('search/progress/<str:job_id>/', views.search_progress, name='search_progress'),
]
