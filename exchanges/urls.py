from rest_framework.routers import DefaultRouter
from .views import ExchangeConnectionViewSet

router = DefaultRouter()
router.register("connections", ExchangeConnectionViewSet, basename="exchange-connection")

urlpatterns = router.urls

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ExchangeConnectionViewSet, AssetListView

router = DefaultRouter()
router.register("connections", ExchangeConnectionViewSet, basename="exchange-connection")

urlpatterns = [
    path("assets/", AssetListView.as_view(), name="asset-list"),
    path("", include(router.urls)),
]
