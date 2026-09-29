"""API des configurations de stratégie."""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import StrategyConfig
from .serializers import StrategyConfigSerializer


class StrategyConfigViewSet(viewsets.ModelViewSet):
    """CRUD + actions start/stop pour les stratégies."""
    serializer_class = StrategyConfigSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Chaque utilisateur ne voit QUE ses propres stratégies
        return StrategyConfig.objects.filter(user=self.request.user).order_by("-id")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        """POST /strategies/{id}/start/ -> active la stratégie."""
        strategy = self.get_object()
        strategy.is_active = True
        strategy.save(update_fields=["is_active"])
        return Response({"status": "started", "id": strategy.id})

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        """POST /strategies/{id}/stop/ -> désactive la stratégie."""
        strategy = self.get_object()
        strategy.is_active = False
        strategy.save(update_fields=["is_active"])
        return Response({"status": "stopped", "id": strategy.id})
