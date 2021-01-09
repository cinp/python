import re
import random
import django
from django.conf import settings
from django.db import DatabaseError, models, transaction
from django.apps import apps
from django.core.exceptions import ObjectDoesNotExist, ValidationError, AppRegistryNotReady
from django.db.models import fields
from django.core.files import File

from cinp.server_common import Converter, Namespace, Model, Action, Paramater, Field, ServerError, checkAuth_true, checkAuth_false, MAP_TYPE_CONVERTER

__MODEL_REGISTRY__ = {}

# TODO: take advantage of .save( update_fields=.... ) on UPDATE

HAS_VIEW_PERMISSION = int( django.get_version().split( '.' )[0] ) >= 2 and int( django.get_version().split( '.' )[1] ) >= 1


def field_model_resolver( django_field ):
  mode = None
  is_array = None

  remote_field = django_field.remote_field

  if django_field.get_internal_type() == 'ManyToManyField':
    # detect if "through" is autogenerated, if not, make the field RO
    # we can't reach into the through model from here, they will have to
    # expose the through model to work with the relationship

    auto_created = None
    try:
      auto_created = remote_field.through._meta.auto_created
    except AttributeError:
      raise ValueError( 'Unknown target model "{0}" make sure it is registered, field: "{1}" model: "{2}"'.format( remote_field.through, django_field.name, django_field.model.__qualname__ ) )

    if auto_created is not None:
      mode = 'RW'
      django_model = remote_field.model

    else:
      mode = 'RO'
      django_model = remote_field.through

    is_array = True

  else:
    django_model = remote_field.model

  if isinstance( django_model, str ):  # this catches the case where there is a circular app refrence, so django has yet to fully map one of the sides when this is run
    try:
      ( app_name, model_name ) = django_model.split( '.' )
    except ValueError:
      raise ValueError( 'Remote Field model name "{0}" in unexpected format, field: "{1}" model: "{2}"'.format( django_model, django_field.name, django_field.model.__qualname__ ) )

    try:  # it is expected that this will throw an exception on the first try, but the late resolve should suceede
      app_config = apps.get_app_config( app_name )
      django_model = app_config.get_model( model_name )
    except AppRegistryNotReady as e:
      raise ValueError( 'App Registry Not Ready: "{0}", when resolving model name "{1}" in unexpected format, field: "{2}" model: "{3}"'.format( e, django_model, django_field.name, django_field.model.__qualname__ ) )

  if not isinstance( django_model, models.base.ModelBase ):
    raise ValueError( 'Remote Field model is not a model type, got "{0}"({1}), field: "{2}" model: "{3}"'.format( django_model, type( django_model ), django_field.name, django_field.model.__qualname__ ) )

  target_model_name = '{0}.{1}'.format( django_model.__module__, django_model.__name__ )
  try:
    model = __MODEL_REGISTRY__[ target_model_name ]
  except KeyError:
    raise ValueError( 'Unknown field model "{0}" make sure it is registered, field: "{1}" model: "{2}"'.format( target_model_name, django_field.name, django_field.model.__qualname__ ) )

  return ( mode, is_array, model )


def paramater_model_resolver( model_name ):
  if not isinstance( model_name, str ):
    model_name = '{0}.{1}'.format( model_name.__module__, model_name.__name__ )

  try:
    model = __MODEL_REGISTRY__[ model_name ]
  except KeyError:
    raise ValueError( 'Unknown paramater model "{0}" make sure it is registered'.format( model_name ) )

  return model


def property_model_resolver( model_name ):
  try:
    model = __MODEL_REGISTRY__[ model_name ]
  except KeyError:
    raise ValueError( 'Unknown paramater model "{0}" make sure it is registered'.format( model_name ) )

  return ( None, None, model )


def paramater_type_to_paramater( paramater_type, extra=None ):
  if paramater_type is None:
    return None

  result = {}

  if isinstance( paramater_type, dict ):
    result[ 'type' ] = paramater_type[ 'type' ]

    try:
      result[ 'doc' ] = paramater_type[ 'doc' ]
    except KeyError:
      pass

    try:
      result[ 'length' ] = paramater_type[ 'length' ]
    except KeyError:
      pass

    try:
      result[ 'is_array' ] = paramater_type[ 'is_array' ]
    except KeyError:
      pass

    try:
      result[ 'choice_list' ] = paramater_type[ 'choice_list' ]
    except KeyError:
      pass

    try:
      result[ 'allowed_scheme_list' ] = paramater_type[ 'allowed_scheme_list' ]
    except KeyError:
      pass

    paramater_model_name = paramater_type.get( 'model', None )
    if paramater_model_name is not None:
      try:
        model = paramater_model_resolver( paramater_model_name )
      except ValueError:  # model_resolver had issues, try late resolving
        result[ 'model' ] = paramater_model_name
        result[ 'model_resolve' ] = paramater_model_resolver
      else:
        result[ 'model' ] = model

  else:
    result[ 'type' ] = paramater_type

  if extra is not None:
    result.update( extra )

  return Paramater( **result )


class DjangoConverter( Converter ):
  def _toPython( self, paramater, cinp_value, transaction ):
    if paramater.type == 'File':
      value = super()._toPython( paramater, cinp_value, transaction )
      if value is None:
        return None

      ( reader, filename ) = value

      if filename is None:
        filename = ''.join( random.choices( '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-', k=20 ) )

      if isinstance( paramater, Field ):
        return File( paramater.django_field.save( filename, reader ) )

      else:
        return File( reader, filename )

    return super()._toPython( paramater, cinp_value, transaction )

  def _fromPython( self, paramater, python_value ):
    if paramater.type == 'Model':
      if python_value is None:
        return None

      return '{0}:{1}:'.format( paramater.model.path, python_value.pk )

    if paramater.type == 'File':
      if python_value is None:
        return None

      return python_value.url

    return super()._fromPython( paramater, python_value )


# decorator for the models
class DjangoCInP():
  def __init__( self, name, version='0.0', doc='' ):
    super().__init__()
    if not re.match( '^[0-9a-zA-Z]*$', name ):
      raise ValueError( 'name "{0}" is invalid'.format( name ) )

    self.name = name
    self.version = version
    self.doc = doc
    self.model_list = []
    self.action_map = {}
    self.check_auth_map = {}
    self.list_filter_map = {}

  def _getTransactionClass( self, cls ):
    if settings.DATABASES[ cls._meta.default_manager.db ][ 'ENGINE' ] == 'django.db.backends.sqlite3':
      return DjangoSQLteTransaction
    return DjangoTransaction

  # this is called to get the namespace to attach to the server
  def getNamespace( self, uri ):
    namespace = Namespace( name=self.name, version=self.version, doc=self.doc, converter=DjangoConverter( uri ) )
    namespace.checkAuth = checkAuth_true
    for model in self.model_list:
      check_auth = self.check_auth_map.get( model.name, None )
      if check_auth is None:
        check_auth = checkAuth_false

      namespace.addElement( model )
      model.checkAuth = check_auth
      for action in self.action_map.get( model.name, [] ):
        action.checkAuth = eval( 'lambda user, verb, id_list: check_auth( user, verb, id_list, "{0}" )'.format( action.name ), { 'check_auth': check_auth } )  # TODO: eval ew, find a better way
        model.addAction( action )

    return namespace

  # decorators
  def model( self, hide_field_list=None, show_field_list=None, property_list=None, constant_set_map=None, not_allowed_verb_list=None, read_only_list=None, cache_length=3600 ):
    def decorator( cls ):
      global __MODEL_REGISTRY__

      name = cls.__qualname__
      meta = cls._meta
      field_list = []
      hide_field_list_ = hide_field_list or []
      show_field_list_ = show_field_list or []
      property_list_ = property_list or []
      read_only_list_ = read_only_list or []
      if hide_field_list_ and show_field_list_:
        raise ValueError( 'hide_field_list and show_field_list are Mutually Exclusive' )

      for django_field in meta.fields + meta.many_to_many:
        if django_field.auto_created:
          continue

        if hide_field_list_ and django_field.name in hide_field_list_:
          continue

        if show_field_list_ and django_field.name not in show_field_list_:
          continue

        kwargs = {
                   'name': django_field.name,
                   'doc': str( django_field.help_text ) if django_field.help_text else None,
                   'required': not django_field.blank and django_field.default == fields.NOT_PROVIDED,
                   'choice_list': [ item[0] for item in django_field.choices ] if django_field.choices else None,
                   'default': django_field.default if django_field.default != fields.NOT_PROVIDED else None
                 }

        if django_field.editable and django_field.name not in read_only_list_:
          kwargs[ 'mode' ] = 'RC' if django_field.primary_key else 'RW'
        else:
          kwargs[ 'mode' ] = 'RO'

        internal_type = django_field.get_internal_type()
        try:
          cinp_type = django_field.cinp_type
          internal_type = None
        except AttributeError:
          cinp_type = None

        if internal_type in ( 'CharField', 'TextField', 'GenericIPAddressField' ) or cinp_type == 'String':
          kwargs[ 'type' ] = 'String'
          kwargs[ 'length' ] = django_field.max_length

        elif internal_type in ( 'DecimalField', 'IntegerField', 'SmallIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'AutField' ) or cinp_type == 'Integer':
          kwargs[ 'type' ] = 'Integer'

        elif internal_type in ( 'FloatField', ) or cinp_type == 'Float':
          kwargs[ 'type' ] = 'Float'

        elif internal_type in ( 'BooleanField', 'NullBooleanField' ) or cinp_type == 'Boolean':
          kwargs[ 'type' ] = 'Boolean'

        elif internal_type in ( 'DateField', 'DateTimeField', 'TimeField' ) or cinp_type == 'DateTime':
          kwargs[ 'type' ] = 'DateTime'

        elif internal_type in [] or cinp_type == 'Map':
          kwargs[ 'type' ] = 'Map'

        elif internal_type in ( 'FileField', 'ImageField' ) or cinp_type == 'File':
          kwargs[ 'type' ] = 'File'
          kwargs[ 'allowed_scheme_list' ] = None  # find some meta location to pass this in

        elif internal_type in ( 'ForeignKey', 'ManyToManyField', 'OneToOneField' ) or cinp_type == 'Modal':
          kwargs[ 'type' ] = 'Model'

          try:
            ( mode, is_array, model ) = field_model_resolver( django_field )

          except ValueError:  # model_resolver had issues, try late resolving
            kwargs[ 'model' ] = django_field
            kwargs[ 'model_resolve' ] = field_model_resolver

          else:
            if mode is not None:
              kwargs[ 'mode' ] = mode

            if is_array is not None:
              kwargs[ 'is_array' ] = is_array

            kwargs[ 'model' ] = model

        else:
          raise ValueError( 'Unknown Field type "{0}"'.format( internal_type ) )

        if 'is_array' not in kwargs:
          try:
            kwargs[ 'is_array' ] = django_field.cinp_is_array
          except AttributeError:
            pass

        field_list.append( Field( **kwargs ) )

      for item in property_list_:
        if isinstance( item, dict ):
          kwargs = {
                     'name': item.get( 'name' ),
                     'doc': item.get( 'doc', None ),
                     'required': False,
                     'default': None,
                     'mode': 'RO',
                     'type': item.get( 'type', 'String' ),
                     'choice_list': item.get( 'choices', None ),
                     'is_array': item.get( 'is_array', False )
                   }

          paramater_model_name = item.get( 'model', None )
          if paramater_model_name is not None:
            try:
              model = paramater_model_resolver( paramater_model_name )
            except ValueError:  # model_resolver had issues, try late resolving
              kwargs[ 'model' ] = paramater_model_name
              kwargs[ 'model_resolve' ] = property_model_resolver   # yes we are sending different than we called
            else:
              kwargs[ 'model' ] = model

        else:
          kwargs = {
                     'name': item,
                     'doc': None,
                     'required': False,
                     'default': None,
                     'mode': 'RO',
                     'type': 'String'
                   }

        field_list.append( Field( **kwargs ) )

      filter_map = {}
      filter_funcs_map = {}
      for filter_name in self.list_filter_map.get( name, {} ):
        filter_funcs_map[ filter_name ] = self.list_filter_map[ name ][ filter_name ][0]
        filter_map[ filter_name ] = self.list_filter_map[ name ][ filter_name ][1]

      try:
        doc = cls.__doc__.strip()
      except AttributeError:
        doc = None

      model = Model( name=name, doc=doc, transaction_class=self._getTransactionClass( cls ), field_list=field_list, list_filter_map=filter_map, constant_set_map=constant_set_map, not_allowed_verb_list=not_allowed_verb_list )
      model._django_model = cls
      model._django_filter_funcs_map = filter_funcs_map
      self.model_list.append( model )
      __MODEL_REGISTRY__[ '{0}.{1}'.format( cls.__module__, cls.__name__ ) ] = model
      MAP_TYPE_CONVERTER[ cls.__name__ ] = lambda a: model.path + ':{0}:'.format( a.pk )
      return cls

    return decorator

  def staticModel( self, not_allowed_verb_list=None, cache_length=3600 ):
    def decorator( cls ):
      name = cls.__qualname__
      not_allowed_verb_list_ = list( set( [ 'LIST', 'GET', 'CREATE', 'UPDATE', 'DELETE' ] ).union( set( not_allowed_verb_list or [] ) ) )

      try:
        doc = cls.__doc__.strip()
      except AttributeError:
        doc = None

      model = Model( name=name, doc=doc, transaction_class=DjangoTransaction, field_list=[], list_filter_map={}, constant_set_map={}, not_allowed_verb_list=not_allowed_verb_list_ )
      self.model_list.append( model )
      return cls

    return decorator

  def action( self, return_type=None, paramater_type_list=None ):  # must decorate the @staticmethod decorator to detect if it is static or not
    def decorator( func ):
      if type( func ).__name__ == 'staticmethod':
        static = True
        func = func.__func__
      else:
        static = False

      paramater_type_list_ = paramater_type_list or []
      ( model_name, name ) = func.__qualname__.split( '.' )
      if model_name not in self.action_map:
        self.action_map[ model_name ] = []

      if static:
        paramater_name_list = func.__code__.co_varnames[ 0:func.__code__.co_argcount ]
      else:
        paramater_name_list = func.__code__.co_varnames[ 1:func.__code__.co_argcount ]  # skip 'self'

      default_list = func.__defaults__
      default_offset = len( paramater_name_list ) - len( default_list or [] )

      if len( paramater_name_list ) != len( paramater_type_list_ ):
        raise ValueError( 'paramater_name_list({0}) is not the same length as paramater_type_list({1}) for "{2}" of "{3}"'.format( len( paramater_name_list ), len( paramater_type_list_ ), name, model_name ) )

      paramater_list = []
      for index in range( 0, len( paramater_type_list_ ) ):
        extra = { 'name': paramater_name_list[ index ] }
        if index >= default_offset:
          extra[ 'default' ] = default_list[ index - default_offset ]

        paramater_list.append( paramater_type_to_paramater( paramater_type_list_[ index ], extra ) )

      return_paramater = paramater_type_to_paramater( return_type )

      try:
        doc = func.__doc__.strip()
      except AttributeError:
        doc = ''

      self.action_map[ model_name ].append( Action( name=name, doc=doc, func=func, return_paramater=return_paramater, paramater_list=paramater_list, static=static ) )
      return func

    return decorator

  def check_auth( self ):
    def decorator( func ):
      if type( func ).__name__ != 'staticmethod':
        raise ValueError( 'check_auth func must be a staticmethod' )

      model_name_parts = func.__func__.__qualname__.split( '.' )
      self.check_auth_map[ '.'.join( model_name_parts[ :-1 ] ) ] = func.__func__

      return func

    return decorator

  @staticmethod
  def basic_auth_check( user, verb, model ):
    if verb in ( 'CALL', 'DESCRIBE' ):
      return True

    app = model._meta.app_label
    model = model._meta.model_name

    if verb in ( 'GET', 'LIST' ):
      if HAS_VIEW_PERMISSION:
        if user.has_perm( '{0}.view_{1}'.format( app, model ) ):
          return True
      else:
        return True

    if verb == 'CREATE' and user.has_perm( '{0}.add_{1}'.format( app, model ) ):
      return True

    if verb == 'UPDATE' and user.has_perm( '{0}.change_{1}'.format( app, model ) ):
      return True

    if verb == 'DELETE' and user.has_perm( '{0}.delete_{1}'.format( app, model ) ):
      return True

    return False

  def list_filter( self, name, paramater_type_list=None ):
    def decorator( func ):
      if type( func ).__name__ != 'staticmethod':
        raise ValueError( 'list_filter func must be a staticmethod' )

      paramater_type_list_ = paramater_type_list or []
      ( model_name, _ ) = func.__func__.__qualname__.split( '.' )

      if model_name not in self.list_filter_map:
        self.list_filter_map[ model_name ] = {}

      paramater_name_list = func.__func__.__code__.co_varnames[ 0:func.__func__.__code__.co_argcount ]

      if len( paramater_name_list ) != len( paramater_type_list_ ):
        raise ValueError( 'paramater_name_list({0}) is not the same length as paramater_type_list({1}) for filter "{2}" of "{3}"'.format( len( paramater_name_list ), len( paramater_type_list_ ), name, model_name ) )

      paramater_map = {}
      for index in range( 0, len( paramater_type_list_ ) ):
        paramater = paramater_type_to_paramater( paramater_type_list_[ index ], { 'name': paramater_name_list[ index ] } )
        paramater_map[ paramater.name ] = paramater

      self.list_filter_map[ model_name ][ name ] = ( func.__func__, paramater_map )

      return func

    return decorator


class DjangoTransaction():  # NOTE: developed on Postgres
  def __init__( self ):
    super().__init__()

  def get( self, model, object_id ):
    try:
      return model._django_model.objects.get( pk=object_id )

    except ObjectDoesNotExist:
      return None

    except ValueError:
      return None  # an invalid pk is indeed 404

  def create( self, model, value_map ):
    target_object = model._django_model()

    multi_multi_list = []
    for name in value_map:
      if model.field_map[ name ].type == 'Model' and model.field_map[ name ].is_array:  # ie: is a ManyToManyField
        if value_map[ name ] is not None:
          multi_multi_list.append( name )
      else:
        setattr( target_object, name, value_map[ name ] )

    try:
      target_object.full_clean()
      target_object.save()
    except ValidationError as e:
      raise ValueError( e.message_dict )
    except DatabaseError as e:
      raise ValueError( str( e ) )

    for name in multi_multi_list:
      getattr( target_object, name ).set( value_map[ name ] )

    return ( target_object.pk, target_object )

  def update( self, model, object_id, value_map ):
    try:
      target_object = model._django_model.objects.get( pk=object_id )
    except ObjectDoesNotExist:
      return None

    for name in value_map:
      if model.field_map[ name ].type == 'Model' and model.field_map[ name ].is_array:  # ie: is a ManyToManyField
        if value_map[ name ] is not None:
          getattr( target_object, name ).set( value_map[ name ] )
      else:
        setattr( target_object, name, value_map[ name ] )

    try:
      target_object.full_clean()
      target_object.save()
    except ValidationError as e:
      raise ValueError( e.message_dict )
    except DatabaseError as e:
      raise ValueError( str( e ) )

    return target_object

  def list( self, model, filter_name, filter_values, position, count ):
    if filter_name is None:
      qs = model._django_model.objects.all()
    else:
      try:
        filter_func = model._django_filter_funcs_map[ filter_name ]
      except KeyError:
        raise ServerError( 'filter_func for "{0}" not found'.format( filter_name ) )  # the filter_name should of allready been checked, something is seriously wrong

      qs = filter_func( **filter_values )

    if not qs.ordered:
      qs = qs.order_by( 'pk' )

    qs = qs.values_list( 'pk' )

    return ( [ item[0] for item in qs[ position:position + count ] ], position, qs.count() )

  def delete( self, model, object_id ):
    try:
      target_object = model._django_model.objects.get( pk=object_id )
    except ObjectDoesNotExist:
      return False

    target_object.delete()
    return True

  def start( self ):
    transaction.set_autocommit( False )

  def commit( self ):
    transaction.commit()
    transaction.set_autocommit( True )

  def abort( self ):
    transaction.rollback()
    transaction.set_autocommit( True )


class DjangoSQLteTransaction( DjangoTransaction ):
  # see https://docs.djangoproject.com/en/3.1/topics/db/transactions/#savepoints-in-sqlite
  def start( self ):
    pass

  def commit( self ):
    pass

  def abort( self ):
    pass
