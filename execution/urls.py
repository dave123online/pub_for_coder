from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    PositionViewSet,
    TradeViewSet,
    PerformanceView,
    BacktestViewSet,
    PortfolioSimulationView,
)

router = DefaultRouter()
router.register(r"backtests", BacktestViewSet, basename="backtest")
router.register(r"positions", PositionViewSet, basename="position")
router.register(r"trades", TradeViewSet, basename="trade")

urlpatterns = [
    path("", include(router.urls)),
    path("performance/", PerformanceView.as_view(), name="performance"),
    path("portfolio-simulation/", PortfolioSimulationView.as_view(), name="portfolio-simulation"),
]
