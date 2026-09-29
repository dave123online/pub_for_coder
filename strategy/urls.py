from rest_framework.routers import DefaultRouter
from .views import StrategyConfigViewSet

router = DefaultRouter()
router.register("strategies", StrategyConfigViewSet, basename="strategy-config")

urlpatterns = router.urls
