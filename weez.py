import re
import sqlite3


class Database(object):
    def __init__(self, database):
        self.database = database
        self.conn = self.get_connection()
    
    def get_connection(self):
        return sqlite3.connect(self.database)
    
    def last_insert_id(self):
        result = self.execute("SELECT last_insert_rowid();")
        return result.fetchone()[0]
    
    def create_table(self, model_class):
        framing = "CREATE TABLE %s (%s);"
        columns = []

        for field in model_class._meta.fields.values():
            columns.append(field.to_sql())
        
        cursor = self.conn.cursor()
        cursor.execute(
            framing % (model_class._meta.db_table, ', '.join(columns))
        )
        self.conn.commit()
    
    def drop_table(self, model_class):
        cursor = self.conn.cursor()
        cursor.execute('DROP TABLE %s;' % model_class._meta.db_table)
        self.conn.commit()


database = Database('test.db')


class QueryResultWrapper(object):
    _result_cache = []
    
    def __init__(self, model, cursor):
        self.model = model
        self.cursor = cursor
    
    def model_from_rowset(self, model_class, row_dict):
        instance = model_class()
        for attr, value in row_dict.iteritems():
            if attr in instance._meta.fields:
                field = instance._meta.fields[attr]
                setattr(instance, attr, field.python_value(value))
            else:
                setattr(instance, attr, value)
        return instance    
    
    def _row_to_dict(self, row, result_cursor):
        return dict((result_cursor.description[i][0], value)
            for i, value in enumerate(row))
    
    def __iter__(self):
        return self
    
    def next(self):
        row = self.cursor.fetchone()
        if row:
            row_dict = self._row_to_dict(row, self.cursor)
            return self.model_from_rowset(self.model, row_dict)
        else:
            raise StopIteration


class BaseQuery(object):
    operations = {
        'lt': '< %s',
        'lte': '<= %s',
        'gt': '> %s',
        'gte': '>= %s',
        'eq': '= %s',
        'in': ' IN (%s)',
        'icontains': "LIKE '%%%s%%' ESCAPE '\\'",
        'contains': "GLOB '*%s*'",
    }
    query_separator = '__'
    requires_commit = True
    
    def __init__(self, model):
        self.model = model
        self.query_context = model
        self._where = {}
        self._joins = []
    
    def parse_query_args(self, **query):
        parsed = {}
        for lhs, rhs in query.iteritems():
            if self.query_separator in lhs:
                lhs, op = lhs.rsplit(self.query_separator, 1)
            else:
                op = 'eq'

            field = self.query_context._meta.get_field_by_name(lhs)
            lookup_value = field.lookup_value(op, rhs)
            parsed[field.name] = self.operations[op] % lookup_value
        
        return parsed
    
    def where(self, query='', **kwargs):
        self._where.setdefault(self.query_context, {})
        if query != '':
            if '__raw__' in self._where[self.query_context]:
                raise ValueError('A raw query has already been specified')
            self._where[self.query_context]['__raw__'] = query
        if kwargs:
            parsed = self.parse_query_args(**kwargs)
            self._where[self.query_context].update(**parsed)
        
        return self
    
    def join(self, model):
        for field in self.model._meta.fields.values():
            if isinstance(field, ForeignKeyField) and field.to == model:
                self._joins.append(model)
                self.query_context = model
        
        return self
    
    def compile_where(self):
        alias_count = 0
        alias_map = {}

        alias_required = len(self._joins) > 0

        joins = list(self._joins)
        if self._where:
            joins.insert(0, self.model)
        
        where_with_alias = []
        computed_joins = []
        
        for i, model in enumerate(joins):
            if alias_required:
                alias_count += 1
                alias_map[model] = 't%d.' % alias_count
            else:
                alias_map[model] = ''
            
            for name, lookup in self._where[model].iteritems():
                if name == '__raw__':
                    where_with_alias.append(lookup)
                else:
                    where_with_alias.append('%s%s %s' % (
                        alias_map[model], name, lookup))
            
            if i > 0:
                from_model = joins[i-1]
                for field in from_model._meta.fields.values():
                    if isinstance(field, ForeignKeyField) and field.to == model:
                        computed_joins.append(
                            'INNER JOIN %s AS %s ON %s%s = %s%s' % (
                                model._meta.db_table,
                                alias_map[model],
                                alias_map[from_model],
                                field.name,
                                alias_map[model],
                                'id'
                            )
                        )
        
        return computed_joins, where_with_alias, alias_map
    
    def raw_execute(self):
        cursor = database.conn.cursor()
        result = cursor.execute(self.sql())
        if self.requires_commit:
            database.conn.commit()
        return result


class SelectQuery(BaseQuery):
    """
    Model.select('*').where(field=val).join(RelModel).where(rel_field=val)
    """
    requires_commit = False
    
    def __init__(self, model, query=None):
        self.query = query or '*'
        self._group_by = []
        self._having = []
        super(SelectQuery, self).__init__(model)
    
    def group_by(self, clause):
        self._group_by.append(clause)
        return self
    
    def having(self, clause):
        self._having.append(clause)
        return self
    
    def sql(self):
        joins, where, alias_map = self.compile_where()
        
        table = self.model._meta.db_table
        if self.model in alias_map:
            table = '%s AS %s' % (table, alias_map[self.model])
            if self.query == '*':
                self.query = '%s*' % alias_map[self.model]
            else:
                pass # handle list of params here
        
        select = 'SELECT %s FROM %s' % (self.query, table)
        joins = '\n'.join(joins)
        where = ' AND '.join(where)
        group_by = ', '.join(self._group_by)
        having = ' AND '.join(self._having)
        
        pieces = [select]
        
        if where:
            pieces.append(joins)
            pieces.append('WHERE %s' % where)
        if group_by:
            pieces.append('GROUP BY %s' % group_by)
        if having:
            pieces.append('HAVING %s' % having)
        
        return ' '.join(pieces)
    
    def execute(self):
        return QueryResultWrapper(self.model, self.raw_execute())
    
    def __iter__(self):
        return self.execute()


class UpdateQuery(BaseQuery):
    """
    Model.update(field=val, field2=val2).where(some_field=some_val)
    """
    def __init__(self, model, **kwargs):
        self.update_query = kwargs
        super(UpdateQuery, self).__init__(model)
    
    def parse_update(self):
        sets = []
        for k, v in self.update_query.iteritems():
            field = self.model._meta.get_field_by_name(k)
            sets.append('%s=%s' % (k, field.lookup_value(None, v)))
        
        return ', '.join(sets)
    
    def sql(self):
        joins, where, alias_map = self.compile_where()
        set_statement = self.parse_update()
        
        update = 'UPDATE %s SET %s' % (self.model._meta.db_table, set_statement)
        where = ' AND '.join(where)
        
        pieces = [update]
        
        if where:
            pieces.append('WHERE %s' % where)
        
        return ' '.join(pieces)
    
    def join(self, *args, **kwargs):
        raise AttributeError('Update queries do not support JOINs in sqlite')
    
    def execute(self):
        result = self.raw_execute()
        return result.rowcount


class DeleteQuery(BaseQuery):
    """
    Model.delete().where(some_field=some_val)
    """
    def sql(self):
        joins, where, alias_map = self.compile_where()
        
        delete = 'DELETE FROM %s' % (self.model._meta.db_table)
        where = ' AND '.join(where)
        
        pieces = [delete]
        
        if where:
            pieces.append('WHERE %s' % where)
        
        return ' '.join(pieces)
    
    def join(self, *args, **kwargs):
        raise AttributeError('Update queries do not support JOINs in sqlite')
    
    def execute(self):
        result = self.raw_execute()
        return result.rowcount


class InsertQuery(BaseQuery):
    """
    Model.insert(field=val, field2=val2)
    """
    def __init__(self, model, **kwargs):
        self.insert_query = kwargs
        super(InsertQuery, self).__init__(model)
    
    def parse_insert(self):
        cols = []
        vals = []
        for k, v in self.insert_query.iteritems():
            field = self.model._meta.get_field_by_name(k)
            cols.append(k)
            vals.append(str(field.lookup_value(None, v)))
        
        return cols, vals
    
    def sql(self):
        cols, vals = self.parse_insert()
        
        insert = 'INSERT INTO %s (%s) VALUES (%s)' % (
            self.model._meta.db_table, ','.join(cols), ','.join(vals))
        
        return insert
    
    def where(self, *args, **kwargs):
        raise AttributeError('Insert queries do not support WHERE clauses')
    
    def join(self, *args, **kwargs):
        raise AttributeError('Insert queries do not support JOINs')
    
    def execute(self):
        result = self.raw_execute()
        return result.lastrowid


class Field(object):
    db_field = ''
    field_template = "%(db_field)s"

    def get_attributes(self):
        return {}
    
    def __init__(self, *args, **kwargs):
        self.attributes = self.get_attributes()
        if 'db_field' not in kwargs:
            kwargs['db_field'] = self.db_field
        self.attributes.update(kwargs)
    
    def add_to_class(self, klass, name):
        self.name = name
        setattr(klass, name, None)
    
    def render_field_template(self):
        return self.field_template % self.attributes
    
    def to_sql(self):
        rendered = self.render_field_template()
        return '"%s" %s' % (self.name, rendered)
    
    def db_value(self, value):
        return value or "NULL"
    
    def python_value(self, value):
        return value
    
    def lookup_value(self, lookup_type, value):
        return self.db_value(value)


class CharField(Field):
    db_field = 'VARCHAR'
    field_template = "%(db_field)s(%(max_length)d) NOT NULL"
    
    def get_attributes(self):
        return {'max_length': 255}
    
    def db_value(self, value):
        value = value or ''
        return value[:self.attributes['max_length']]
    
    def lookup_value(self, lookup_type, value):
        if lookup_type in ('contains', 'icontains'):
            return self.db_value(value)
        else:
            return '"%s"' % self.db_value(value)
    

class TextField(Field):
    db_field = 'TEXT'
    
    def db_value(self, value):
        return value or ''
    
    def lookup_value(self, lookup_type, value):
        if lookup_type in ('contains', 'icontains'):
            return self.db_value(value)
        else:
            return '"%s"' % self.db_value(value)


class DateTimeField(Field):
    db_field = 'DATETIME'
    field_template = "%(db_field)s"
    
    def python_value(self, value):
        if value is not None:
            return datetime(*time.strptime(value, '%Y-%m-%d %H:%M:%S')[:6])
    
    def db_value(self, value):
        if value is not None:
            return '"%s"' % value.strftime('%Y-%m-%d %H:%M:%S')
        return "NULL"


class IntegerField(Field):
    db_field = 'INTEGER'
    field_template = "%(db_field)s NOT NULL"
    
    def db_value(self, value):
        return value or 0
    
    def python_value(self, value):
        return int(value or 0)


class FloatField(Field):
    db_field = 'REAL'
    field_template = "%(db_field)s NOT NULL"
    
    def db_value(self, value):
        return value or 0.0
    
    def python_value(self, value):
        return float(value or 0)


class PrimaryKeyField(IntegerField):
    field_template = "%(db_field)s NOT NULL PRIMARY KEY"


class ForeignRelatedObject(object):
    _object_cache = None
    
    def __init__(self, to, name):
        self.field_name = name
        self.to = to
    
    def __get__(self, instance, instance_type=None):
        id = getattr(instance, self.field_name, 0)
        qr = self.to._meta.select().where(id=id).execute()
        return qr.next()
    
    def __set__(self, instance, obj):
        assert isinstance(obj, self.to), "Cannot assign %s, invalid type" % obj
        setattr(instance, self.field_name, obj.id)


class ReverseForeignRelatedObject(object):
    def __init__(self, related_model, name):
        self.field_name = name
        self.related_model = related_model
    
    def __get__(self, instance, instance_type=None):
        query = {self.field_name: instance.id}
        qr = self.related_model._meta.select().where(**query)
        return qr


class ForeignKeyField(IntegerField):
    field_template = '%(db_field)s NOT NULL REFERENCES "%(to_table)s" ("id")'
    
    def __init__(self, to, *args, **kwargs):
        self.to = to
        kwargs['to_table'] = to._meta.db_table
        super(ForeignKeyField, self).__init__(*args, **kwargs)
    
    def add_to_class(self, klass, name):
        self.descriptor = name
        self.name = name + '_id'
        self.related_name = klass._meta.db_table + '_set'
        setattr(klass, self.descriptor, ForeignRelatedObject(self.to, self.name))
        setattr(klass, self.name, None)
        
        reverse_rel = ReverseForeignRelatedObject(klass, self.name)
        setattr(self.to, self.related_name, reverse_rel)
    
    def lookup_value(self, lookup_type, value):
        if isinstance(value, Model):
            return value.id
        return value or "NULL"


class BaseModel(type):
    def __new__(cls, name, bases, attrs):
        cls = super(BaseModel, cls).__new__(cls, name, bases, attrs)
    
        class Meta(object):
            fields = {}
            
            def __init__(self, model_class):
                self.model_class = model_class
            
            def get_field_by_name(self, name):
                if name in self.fields:
                    return self.fields[name]
                raise AttributeError('Field named %s not found' % name)
            
            def get_related_field_for_model(self, model):
                for field in self.fields.values():
                    if isinstance(field, ForeignKeyField) and field.to == model:
                        return field
            
            def get_field_dict(self, instance):
                field_val = lambda f: (f.name, getattr(instance, f.name))
                pairs = map(field_val, self.fields.values())
                return dict(pairs)
            
            def select(self, query=None):
                return SelectQuery(self.model_class, query)
            
            def save(self, instance):
                field_dict = self.get_field_dict(instance)
                field_dict.pop('id')
                if instance.id:
                    update = UpdateQuery(self.model_class, **field_dict).where(id=instance.id)
                    update.execute()
                else:
                    insert = InsertQuery(self.model_class, **field_dict)
                    instance.id = insert.execute()
            
            def delete(self, instance):
                if instance.id:
                    delete = DeleteQuery(self.model_class).where(id=instance.id)
                    return database.execute(delete)
            
            
        
        _meta = Meta(cls)
        setattr(cls, '_meta', _meta)
        
        _meta.db_table = re.sub('[^a-z]+', '_', cls.__name__.lower())
        
        has_primary_key = False

        for name, attr in cls.__dict__.items():
            if isinstance(attr, Field):
                attr.add_to_class(cls, name)
                _meta.fields[attr.name] = attr
                if isinstance(attr, PrimaryKeyField):
                    has_primary_key = True
        
        if not has_primary_key:
            pk = PrimaryKeyField()
            pk.add_to_class(cls, 'id')
            _meta.fields['id'] = pk
                
        if hasattr(cls, '__unicode__'):
            setattr(cls, '__repr__', lambda self: '<%s: %s>' % (
                self.__class__.__name__, self.__unicode__()))
        
        return cls


class Model(object):
    __metaclass__ = BaseModel
    
    def __init__(self, *args, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
    
    def save(self):
        return self._meta.save(self)
