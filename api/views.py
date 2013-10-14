"""
RESTful view classes for presenting Deis API objects.
"""
# pylint: disable=R0901,R0904

from __future__ import unicode_literals
import json

from Crypto.PublicKey import RSA
from celery.canvas import group
from django.contrib.auth.models import AnonymousUser, User
from django.utils import timezone
from guardian.shortcuts import get_objects_for_user
from rest_framework import permissions, status, viewsets
from rest_framework.authentication import BaseAuthentication
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST

from api import models
from api import serializers
from api import tasks


class AnonymousAuthentication(BaseAuthentication):

    def authenticate(self, request):
        """
        Authenticate the request and return a two-tuple of (user, token).
        """
        user = AnonymousUser()
        return user, None


class IsAnonymous(permissions.BasePermission):
    """
    Object-level permission to allow anonymous users.
    """

    def has_permission(self, request, view):
        """
        Return `True` if permission is granted, `False` otherwise.
        """
        if type(request.user) == AnonymousUser:
            return True
        return False


class IsOwner(permissions.BasePermission):
    """
    Object-level permission to allow only owners of an object to access it.
    Assumes the model instance has an `owner` attribute.
    """

    def has_object_permission(self, request, view, obj):
        if hasattr(obj, 'owner'):
            return obj.owner == request.user
        elif hasattr(obj, 'formation'):
            return obj.formation.owner == request.user
        else:
            return False


class UserRegistrationView(viewsets.GenericViewSet,
                           viewsets.mixins.CreateModelMixin):
    model = User

    authentication_classes = (AnonymousAuthentication,)
    permission_classes = (IsAnonymous,)
    serializer_class = serializers.UserSerializer

    def post_save(self, user, created=False):
        """Seed both `Providers` and `Flavors` after registration."""
        if created:
            models.Provider.objects.seed(user)
            models.Flavor.objects.seed(user)

    def pre_save(self, obj):
        """Replicate UserManager.create_user functionality."""
        now = timezone.now()
        obj.last_login = now
        obj.date_joined = now
        obj.is_active = True
        obj.email = User.objects.normalize_email(obj.email)
        obj.set_password(obj.password)


class UserCancellationView(viewsets.GenericViewSet,
                           viewsets.mixins.DestroyModelMixin):
    model = User

    permission_classes = (permissions.IsAuthenticated,)

    def destroy(self, request, *args, **kwargs):
        obj = self.request.user
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OwnerViewSet(viewsets.ModelViewSet):
    """Scope views to an `owner` attribute."""

    permission_classes = (permissions.IsAuthenticated, IsOwner)

    def pre_save(self, obj):
        obj.owner = self.request.user

    def get_queryset(self, **kwargs):
        """Filter all querysets by an `owner` attribute.
        """
        return self.model.objects.filter(owner=self.request.user)


class KeyViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.Key`."""

    model = models.Key
    serializer_class = serializers.KeySerializer
    lookup_field = 'id'


class ProviderViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.Provider`."""

    model = models.Provider
    serializer_class = serializers.ProviderSerializer
    lookup_field = 'id'


class FlavorViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.Flavor`."""

    model = models.Flavor
    serializer_class = serializers.FlavorSerializer
    lookup_field = 'id'

    def update(self, request, *args, **kwargs):
        """
        Override default update behavior to ensure that the params
        field is handled as a dict merge.
        """
        params = self.get_object().params
        new_params = json.loads(request.DATA.get('params', '{}'))
        params.update(new_params)
        # remove param if we provided a null value
        [params.pop(k) for k, v in params.items() if v is None]
        request.DATA['params'] = json.dumps(params)
        return super(FlavorViewSet, self).update(request, *args, **kwargs)


class FormationViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.Formation`."""

    model = models.Formation
    serializer_class = serializers.FormationSerializer
    lookup_field = 'id'

    def get_queryset(self, **kwargs):
        """
        Filter Formations by `owner` attribute or the
        `api.use_formation` permission.
        """
        return super(FormationViewSet, self).get_queryset(**kwargs) | \
            get_objects_for_user(self.request.user, 'api.use_formation')

    def post_save(self, formation, created=False, **kwargs):
        if created:
            formation.build()

    def scale(self, request, **kwargs):
        new_structure = {}
        try:
            for target, count in request.DATA.items():
                new_structure[target] = int(count)
        except ValueError:
            return Response('Invalid scaling format', status=HTTP_400_BAD_REQUEST)
        # check for empty credentials
        for p in models.Provider.objects.filter(owner=request.user):
            if p.creds:
                break
        else:
            return Response('No provider credentials available', status=HTTP_400_BAD_REQUEST)
        formation = self.get_object()
        try:
            databag = models.Node.objects.scale(formation, new_structure)
        except (models.Layer.DoesNotExist, EnvironmentError) as err:
            return Response(str(err), status=status.HTTP_400_BAD_REQUEST)
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def balance(self, request, **kwargs):
        formation = self.get_object()
        databag = models.Container.objects.balance(formation)
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def calculate(self, request, **kwargs):
        formation = self.get_object()
        databag = formation.calculate()
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def converge(self, request, **kwargs):
        formation = self.get_object()
        databag = formation.converge()
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def destroy(self, request, **kwargs):
        formation = self.get_object()
        formation.destroy()
        tasks.converge_controller.delay().wait()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FormationScopedViewSet(OwnerViewSet):

    def get_queryset(self, **kwargs):
        formations = models.Formation.objects.filter(
            owner=self.request.user)
        formation = get_object_or_404(formations, id=self.kwargs['id'])
        return self.model.objects.filter(owner=self.request.user,
                                         formation=formation)


class FormationLayerViewSet(FormationScopedViewSet):
    """RESTful views for :class:`~api.models.Layer`."""

    model = models.Layer
    serializer_class = serializers.LayerSerializer

    def get_object(self, *args, **kwargs):
        qs = self.get_queryset(**kwargs)
        obj = get_object_or_404(qs, id=self.kwargs['layer'])
        return obj

    def create(self, request, **kwargs):
        request._data = request.DATA.copy()
        formation = models.Formation.objects.get(
            owner=self.request.user, id=self.kwargs['id'])
        request.DATA['formation'] = formation.id
        if not 'ssh_private_key' in request.DATA and not 'ssh_public_key' in request.DATA:
            # SECURITY: figure out best way to get keys with proper entropy
            key = RSA.generate(2048)
            request.DATA['ssh_private_key'] = key.exportKey('PEM')
            request.DATA['ssh_public_key'] = key.exportKey('OpenSSH')
        return OwnerViewSet.create(self, request, **kwargs)

    def post_save(self, layer, created=False, **kwargs):
        if created:
            layer.build()

    def destroy(self, request, **kwargs):
        layer = self.get_object()
        layer.destroy()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FormationNodeViewSet(FormationScopedViewSet):
    """RESTful views for :class:`~api.models.Node`."""

    model = models.Node
    serializer_class = serializers.NodeSerializer

    def get_object(self, *args, **kwargs):
        qs = self.get_queryset(**kwargs)
        obj = get_object_or_404(qs, id=self.kwargs['node'])
        return obj

    def add(self, request, **kwargs):
        fqdn = request.DATA['fqdn']
        formation = models.Formation.objects.get(
            owner=self.request.user, id=self.kwargs['id'])
        layer = models.Layer.objects.get(
            owner=self.request.user, id=request.DATA['layer'])
        if self.model.objects.filter(fqdn=fqdn, formation=formation, layer=layer).exists():
            msg = "A node with fqdn={} already exists in the {} formation".format(fqdn, formation)
            return Response(data=msg, status=status.HTTP_409_CONFLICT)
        node = models.Node.objects.new(formation, layer, fqdn)
        node.build()
        return Response(status=status.HTTP_201_CREATED)

    def destroy(self, request, **kwargs):
        node = self.get_object()
        node.destroy()
        return Response(status=status.HTTP_204_NO_CONTENT)


class NodeViewSet(FormationNodeViewSet):
    """RESTful views for :class:`~api.models.Node`."""

    def get_queryset(self, **kwargs):
        return self.model.objects.filter(owner=self.request.user)

    def converge(self, request, **kwargs):
        node = self.get_object()
        output, _ = node.converge()
        return Response(output, status=status.HTTP_200_OK, content_type='text/plain')


class AppViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.App`."""

    model = models.App
    serializer_class = serializers.AppSerializer
    lookup_field = 'id'

    def get_queryset(self, **kwargs):
        """Filter Apps by `owner` attribute or `api.change_app` permission.
        """
        return super(AppViewSet, self).get_queryset(**kwargs) | \
            get_objects_for_user(self.request.user, 'api.change_app')

    def post_save(self, app, created=False, **kwargs):
        if created:
            app.build()
        group(*[tasks.converge_formation.si(app.formation),  # @UndefinedVariable
                tasks.converge_controller.si()]).apply_async().join()  # @UndefinedVariable

    def pre_save(self, app, created=False, **kwargs):
        if not app.pk and not app.formation.domain and app.formation.app_set.count() > 0:
            raise EnvironmentError('Formation does not support multiple apps')
        return super(AppViewSet, self).pre_save(app, **kwargs)

    def create(self, request, **kwargs):
        try:
            return OwnerViewSet.create(self, request, **kwargs)
        except EnvironmentError as e:
            return Response(str(e), status=HTTP_400_BAD_REQUEST)

    def scale(self, request, **kwargs):
        new_structure = {}
        try:
            for target, count in request.DATA.items():
                new_structure[target] = int(count)
        except ValueError:
            return Response('Invalid scaling format', status=HTTP_400_BAD_REQUEST)
        app = self.get_object()
        try:
            models.Container.objects.scale(app, new_structure)
        except EnvironmentError as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        # save new structure now that scaling was successful
        app.containers.update(new_structure)
        app.save()
        databag = app.converge()
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def calculate(self, request, **kwargs):
        app = self.get_object()
        databag = app.calculate()
        return Response(databag, status=status.HTTP_200_OK,
                        content_type='application/json')

    def logs(self, request, **kwargs):
        app = self.get_object()
        try:
            logs = app.logs()
        except EnvironmentError:
            return Response("No logs for {}".format(app.id),
                            status=status.HTTP_404_NOT_FOUND,
                            content_type='text/plain')
        return Response(logs, status=status.HTTP_200_OK,
                        content_type='text/plain')

    def run(self, request, **kwargs):
        app = self.get_object()
        command = request.DATA['command']
        try:
            output_and_rc = app.run(command)
        except EnvironmentError as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        return Response(output_and_rc, status=status.HTTP_200_OK,
                        content_type='text/plain')

    def destroy(self, request, **kwargs):
        app = self.get_object()
        app.destroy()
        group(*[tasks.converge_formation.si(app.formation),  # @UndefinedVariable
                tasks.converge_controller.si()]).apply_async().join()  # @UndefinedVariable
        return Response(status=status.HTTP_204_NO_CONTENT)


class BaseAppViewSet(OwnerViewSet):

    def get_queryset(self, **kwargs):
        app = get_object_or_404(models.App.objects.filter(
            owner=self.request.user, id=self.kwargs['id']))
        return self.model.objects.filter(owner=self.request.user, app=app)

    def get_object(self, *args, **kwargs):
        return self.get_queryset().latest('created')


class AppConfigViewSet(BaseAppViewSet):
    """RESTful views for :class:`~api.models.Config`."""

    model = models.Config
    serializer_class = serializers.ConfigSerializer

    def post_save(self, obj, created=False):
        if created:
            models.release_signal.send(
                sender=self, config=obj, app=obj.app,
                user=self.request.user)
            # converge after each config update
            obj.app.formation.converge()

    def create(self, request, *args, **kwargs):
        request._data = request.DATA.copy()
        # assume an existing config object exists
        obj = self.get_object()
        # increment version and use the same formation
        request.DATA['version'] = obj.version + 1
        request.DATA['app'] = obj.app
        # merge config values
        values = obj.values.copy()
        provided = json.loads(request.DATA['values'])
        values.update(provided)
        # remove config keys if we provided a null value
        [values.pop(k) for k, v in provided.items() if v is None]
        request.DATA['values'] = values
        return super(OwnerViewSet, self).create(request, *args, **kwargs)


class AppBuildViewSet(BaseAppViewSet):
    """RESTful views for :class:`~api.models.Build`."""

    model = models.Build
    serializer_class = serializers.BuildSerializer

    def post_save(self, obj, created=False):
        if created:
            models.release_signal.send(
                sender=self, build=obj, app=obj.app,
                user=self.request.user)

    def create(self, request, *args, **kwargs):
        request._data = request.DATA.copy()
        app = models.App.objects.get(owner=self.request.user, id=self.kwargs['id'])
        request.DATA['app'] = app
        return super(OwnerViewSet, self).create(request, *args, **kwargs)


class AppReleaseViewSet(BaseAppViewSet):
    """RESTful views for :class:`~api.models.Release`."""

    model = models.Release
    serializer_class = serializers.ReleaseSerializer


class AppContainerViewSet(OwnerViewSet):
    """RESTful views for :class:`~api.models.Container`."""

    model = models.Container
    serializer_class = serializers.ContainerSerializer

    def get_queryset(self, **kwargs):
        app = get_object_or_404(models.App.objects.filter(
            owner=self.request.user, id=self.kwargs['id']))
        qs = self.model.objects.filter(owner=self.request.user, app=app)
        container_type = self.kwargs.get('type')
        if container_type:
            qs = qs.filter(type=container_type)
        return qs

    def get_object(self, *args, **kwargs):
        qs = self.get_queryset(**kwargs)
        obj = qs.get(num=self.kwargs['num'])
        return obj
