#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
    ambari_jinja2.environment
    ~~~~~~~~~~~~~~~~~~

    Provides a class that holds runtime and parsing time options.

    :copyright: (c) 2010 by the Jinja Team.
    :license: BSD, see LICENSE for more details.
"""
import os
import sys
from ambari_jinja2 import nodes
from ambari_jinja2.defaults import *
from ambari_jinja2.lexer import get_lexer, TokenStream
from ambari_jinja2.parser import Parser
from ambari_jinja2.optimizer import optimize
from ambari_jinja2.compiler import generate
from ambari_jinja2.runtime import Undefined, new_context
from ambari_jinja2.exceptions import TemplateSyntaxError, TemplateNotFound, \
     TemplatesNotFound
from ambari_jinja2.utils import import_string, LRUCache, Markup, missing, \
     concat, consume, internalcode, _encode_filename
from functools import reduce


# for direct template usage we have up to ten living environments
_spontaneous_environments = LRUCache(10)

# the function to create jinja traceback objects.  This is dynamically
# imported on the first exception in the exception handler.
_make_traceback = None


def get_spontaneous_environment(*args):
    """Return a new spontaneous environment.  A spontaneous environment is an
    unnamed and unaccessible (in theory) environment that is used for
    templates generated from a string and not from the file system.
    """
    try:
        env = _spontaneous_environments.get(args)
    except TypeError:
        return Environment(*args)
    if env is not None:
        return env
    _spontaneous_environments[args] = env = Environment(*args)
    env.shared = True
    return env


def create_cache(size):
    """Return the cache class for the given size."""
    if size == 0:
        return None
    if size < 0:
        return {}
    return LRUCache(size)


def copy_cache(cache):
    """Create an empty copy of the given cache."""
    if cache is None:
        return None
    elif type(cache) is dict:
        return {}
    return LRUCache(cache.capacity)


def load_extensions(environment, extensions):
    """Load the extensions from the list and bind it to the environment.
    Returns a dict of instanciated environments.
    """
    result = {}
    for extension in extensions:
        if isinstance(extension, str):
            extension = import_string(extension)
        result[extension.identifier] = extension(environment)
    return result


def _environment_sanity_check(environment):
    """Perform a sanity check on the environment."""
    assert issubclass(environment.undefined, Undefined), 'undefined must ' \
           'be a subclass of undefined because filters depend on it.'
    assert environment.block_start_string != \
           environment.variable_start_string != \
           environment.comment_start_string, 'block, variable and comment ' \
           'start strings must be different'
    assert environment.newline_sequence in ('\r', '\r\n', '\n'), \
           'newline_sequence set to unknown line ending string.'
    return environment


class Environment(object):
    r"""The core component of Jinja is the `Environment`.  It contains
    important shared variables like configuration, filters, tests,
    globals and others.  Instances of this class may be modified if
    they are not shared and if no template was loaded so far.
    Modifications on environments after the first template was loaded
    will lead to surprising effects and undefined behavior.

    Here the possible initialization parameters:

        `block_start_string`
            The string marking the begin of a block.  Defaults to ``'{%'``.

        `block_end_string`
            The string marking the end of a block.  Defaults to ``'%}'``.

        `variable_start_string`
            The string marking the begin of a print statement.
            Defaults to ``'{{'``.

        `variable_end_string`
            The string marking the end of a print statement.  Defaults to
            ``'}}'``.

        `comment_start_string`
            The string marking the begin of a comment.  Defaults to ``'{#'``.

        `comment_end_string`
            The string marking the end of a comment.  Defaults to ``'#}'``.

        `line_statement_prefix`
            If given and a string, this will be used as prefix for line based
            statements.  See also :ref:`line-statements`.

        `line_comment_prefix`
            If given and a string, this will be used as prefix for line based
            based comments.  See also :ref:`line-statements`.

            .. versionadded:: 2.2

        `trim_blocks`
            If this is set to ``True`` the first newline after a block is
            removed (block, not variable tag!).  Defaults to `False`.

        `newline_sequence`
            The sequence that starts a newline.  Must be one of ``'\r'``,
            ``'\n'`` or ``'\r\n'``.  The default is ``'\n'`` which is a
            useful default for Linux and OS X systems as well as web
            applications.

        `extensions`
            List of Jinja extensions to use.  This can either be import paths
            as strings or extension classes.  For more information have a
            look at :ref:`the extensions documentation <jinja-extensions>`.

        `optimized`
            should the optimizer be enabled?  Default is `True`.

        `undefined`
            :class:`Undefined` or a subclass of it that is used to represent
            undefined values in the template.

        `finalize`
            A callable that can be used to process the result of a variable
            expression before it is output.  For example one can convert
            `None` implicitly into an empty string here.

        `autoescape`
            If set to true the XML/HTML autoescaping feature is enabled by
            default.  For more details about auto escaping see
            :class:`~ambari_jinja2.utils.Markup`.  As of Jinja 2.4 this can also
            be a callable that is passed the template name and has to
            return `True` or `False` depending on autoescape should be
            enabled by default.

            .. versionchanged:: 2.4
               `autoescape` can now be a function

        `loader`
            The template loader for this environment.

        `cache_size`
            The size of the cache.  Per default this is ``50`` which means
            that if more than 50 templates are loaded the loader will clean
            out the least recently used template.  If the cache size is set to
            ``0`` templates are recompiled all the time, if the cache size is
            ``-1`` the cache will not be cleaned.

        `auto_reload`
            Some loaders load templates from locations where the template
            sources may change (ie: file system or database).  If
            `auto_reload` is set to `True` (default) every time a template is
            requested the loader checks if the source changed and if yes, it
            will reload the template.  For higher performance it's possible to
            disable that.

        `bytecode_cache`
            If set to a bytecode cache object, this object will provide a
            cache for the internal Jinja bytecode so that templates don't
            have to be parsed if they were not changed.

            See :ref:`bytecode-cache` for more information.
    """

    #: if this environment is sandboxed.  Modifying this variable won't make
    #: the environment sandboxed though.  For a real sandboxed environment
    #: have a look at ambari_jinja2.sandbox
    sandboxed = False

    #: True if the environment is just an overlay
    overlayed = False

    #: the environment this environment is linked to if it is an overlay
    linked_to = None

    #: shared environments have this set to `True`.  A shared environment
    #: must not be modified
    shared = False

    #: these are currently EXPERIMENTAL undocumented features.
    exception_handler = None
    exception_formatter = None

    def __init__(self,
                 block_start_string=BLOCK_START_STRING,
                 block_end_string=BLOCK_END_STRING,
                 variable_start_string=VARIABLE_START_STRING,
                 variable_end_string=VARIABLE_END_STRING,
                 comment_start_string=COMMENT_START_STRING,
                 comment_end_string=COMMENT_END_STRING,
                 line_statement_prefix=LINE_STATEMENT_PREFIX,
                 line_comment_prefix=LINE_COMMENT_PREFIX,
                 trim_blocks=TRIM_BLOCKS,
                 newline_sequence=NEWLINE_SEQUENCE,
                 extensions=(),
                 optimized=True,
                 undefined=Undefined,
                 finalize=None,
                 autoescape=False,
                 loader=None,
                 cache_size=50,
                 auto_reload=True,
                 bytecode_cache=None):
        # !!Important notice!!
        #   The constructor accepts quite a few arguments that should be
        #   passed by keyword rather than position.  However it's important to
        #   not change the order of arguments because it's used at least
        #   internally in those cases:
        #       -   spontaneus environments (i18n extension and Template)
        #       -   unittests
        #   If parameter changes are required only add parameters at the end
        #   and don't change the arguments (or the defaults!) of the arguments
        #   existing already.

        # lexer / parser information
        self.block_start_string = block_start_string
        self.block_end_string = block_end_string
        self.variable_start_string = variable_start_string
        self.variable_end_string = variable_end_string
        self.comment_start_string = comment_start_string
        self.comment_end_string = comment_end_string
        self.line_statement_prefix = line_statement_prefix
        self.line_comment_prefix = line_comment_prefix
        self.trim_blocks = trim_blocks
        self.newline_sequence = newline_sequence

        # runtime information
        self.undefined = undefined
        self.optimized = optimized
        self.finalize = finalize
        self.autoescape = autoescape

        # defaults
        self.filters = DEFAULT_FILTERS.copy()
        self.tests = DEFAULT_TESTS.copy()
        self.globals = DEFAULT_NAMESPACE.copy()

        # set the loader provided
        self.loader = loader
        self.bytecode_cache = None
        self.cache = create_cache(cache_size)
        self.bytecode_cache = bytecode_cache
        self.auto_reload = auto_reload

        # load extensions
        self.extensions = load_extensions(self, extensions)

        _environment_sanity_check(self)

    def add_extension(self, extension):
        """Adds an extension after the environment was created.

        .. versionadded:: 2.5
        """
        self.extensions.update(load_extensions(self, [extension]))

    def extend(self, **attributes):
        """Add the items to the instance of the environment if they do not exist
        yet.  This is used by :ref:`extensions <writing-extensions>` to register
        callbacks and configuration values without breaking inheritance.
        """
        for key, value in list(attributes.items()):
            if not hasattr(self, key):
                setattr(self, key, value)

    def overlay(self, block_start_string=missing, block_end_string=missing,
                variable_start_string=missing, variable_end_string=missing,
                comment_start_string=missing, comment_end_string=missing,
                line_statement_prefix=missing, line_comment_prefix=missing,
                trim_blocks=missing, extensions=missing, optimized=missing,
                undefined=missing, finalize=missing, autoescape=missing,
                loader=missing, cache_size=missing, auto_reload=missing,
                bytecode_cache=missing):
        """Create a new overlay environment that shares all the data with the
        current environment except of cache and the overridden attributes.
        Extensions cannot be removed for an overlayed environment.  An overlayed
        environment automatically gets all the extensions of the environment it
        is linked to plus optional extra extensions.

        Creating overlays should happen after the initial environment was set
        up completely.  Not all attributes are truly linked, some are just
        copied over so modifications on the original environment may not shine
        through.
        """
        args = dict(locals())
        del args['self'], args['cache_size'], args['extensions']

        rv = object.__new__(self.__class__)
        rv.__dict__.update(self.__dict__)
        rv.overlayed = True
        rv.linked_to = self

        for key, value in args.items():
            if value is not missing:
                setattr(rv, key, value)

        if cache_size is not missing:
            rv.cache = create_cache(cache_size)
        else:
            rv.cache = copy_cache(self.cache)

        rv.extensions = {}
        for key, value in self.extensions.items():
            rv.extensions[key] = value.bind(rv)
        if extensions is not missing:
            rv.extensions.update(load_extensions(rv, extensions))

        return _environment_sanity_check(rv)

    lexer = property(get_lexer, doc="The lexer for this environment.")

    def iter_extensions(self):
        """Iterates over the extensions by priority."""
        return iter(sorted(self.extensions.values(),
                           key=lambda x: x.priority))

    def getitem(self, obj, argument):
        """Get an item or attribute of an object but prefer the item."""
        try:
            return obj[argument]
        except (TypeError, LookupError):
            if isinstance(argument, str):
                try:
                    attr = str(argument)
                except:
                    pass
                else:
                    try:
                        return getattr(obj, attr)
                    except AttributeError:
                        pass
            return self.undefined(obj=obj, name=argument)

    def getattr(self, obj, attribute):
        """Get an item or attribute of an object but prefer the attribute.
        Unlike :meth:`getitem` the attribute *must* be a bytestring.
        """
        try:
            return getattr(obj, attribute)
        except AttributeError:
            pass
        try:
            return obj[attribute]
        except (TypeError, LookupError, AttributeError):
            return self.undefined(obj=obj, name=attribute)

    @internalcode
    def parse(self, source, name=None, filename=None):
        """Parse the sourcecode and return the abstract syntax tree.  This
        tree of nodes is used by the compiler to convert the template into
        executable source- or bytecode.  This is useful for debugging or to
        extract information from templates.

        If you are :ref:`developing Jinja2 extensions <writing-extensions>`
        this gives you a good overview of the node tree generated.
        """
        try:
            return self._parse(source, name, filename)
        except TemplateSyntaxError:
            exc_info = sys.exc_info()
        self.handle_exception(exc_info, source_hint=source)

    def _parse(self, source, name, filename):
        """Internal parsing function used by `parse` and `compile`."""
        return Parser(self, source, name, _encode_filename(filename)).parse()

    def lex(self, source, name=None, filename=None):
        """Lex the given sourcecode and return a generator that yields
        tokens as tuples in the form ``(lineno, token_type, value)``.
        This can be useful for :ref:`extension development <writing-extensions>`
        and debugging templates.

        This does not perform preprocessing.  If you want the preprocessing
        of the extensions to be applied you have to filter source through
        the :meth:`preprocess` method.
        """
        source = str(source)
        try:
            return self.lexer.tokeniter(source, name, filename)
        except TemplateSyntaxError:
            exc_info = sys.exc_info()
        self.handle_exception(exc_info, source_hint=source)

    def preprocess(self, source, name=None, filename=None):
        """Preprocesses the source with all extensions.  This is automatically
        called for all parsing and compiling methods but *not* for :meth:`lex`
        because there you usually only want the actual source tokenized.
        """
        return reduce(lambda s, e: e.preprocess(s, name, filename),
                      self.iter_extensions(), str(source))

    def _tokenize(self, source, name, filename=None, state=None):
        """Called by the parser to do the preprocessing and filtering
        for all the extensions.  Returns a :class:`~ambari_jinja2.lexer.TokenStream`.
        """
        source = self.preprocess(source, name, filename)
        stream = self.lexer.tokenize(source, name, filename, state)
        for ext in self.iter_extensions():
            stream = ext.filter_stream(stream)
            if not isinstance(stream, TokenStream):
                stream = TokenStream(stream, name, filename)
        return stream

    def _generate(self, source, name, filename, defer_init=False):
        """Internal hook that can be overriden to hook a different generate
        method in.

        .. versionadded:: 2.5
        """
        return generate(source, self, name, filename, defer_init=defer_init)

    def _compile(self, source, filename):
        """Internal hook that can be overriden to hook a different compile
        method in.

        .. versionadded:: 2.5
        """
        return compile(source, filename, 'exec')

    @internalcode
    def compile(self, source, name=None, filename=None, raw=False,
                defer_init=False):
        """Compile a node or template source code.  The `name` parameter is
        the load name of the template after it was joined using
        :meth:`join_path` if necessary, not the filename on the file system.
        the `filename` parameter is the estimated filename of the template on
        the file system.  If the template came from a database or memory this
        can be omitted.

        The return value of this method is a python code object.  If the `raw`
        parameter is `True` the return value will be a string with python
        code equivalent to the bytecode returned otherwise.  This method is
        mainly used internally.

        `defer_init` is use internally to aid the module code generator.  This
        causes the generated code to be able to import without the global
        environment variable to be set.

        .. versionadded:: 2.4
           `defer_init` parameter added.
        """
        source_hint = None
        try:
            if isinstance(source, str):
                source_hint = source
                source = self._parse(source, name, filename)
            if self.optimized:
                source = optimize(source, self)
            source = self._generate(source, name, filename,
                                    defer_init=defer_init)
            if raw:
                return source
            if filename is None:
                filename = '<template>'
            else:
                filename = _encode_filename(filename)
            return self._compile(source, filename)
        except TemplateSyntaxError:
            exc_info = sys.exc_info()
        self.handle_exception(exc_info, source_hint=source)

    def compile_expression(self, source, undefined_to_none=True):
        """A handy helper method that returns a callable that accepts keyword
        arguments that appear as variables in the expression.  If called it
        returns the result of the expression.

        This is useful if applications want to use the same rules as Jinja
        in template "configuration files" or similar situations.

        Example usage:

        >>> env = Environment()
        >>> expr = env.compile_expression('foo == 42')
        >>> expr(foo=23)
        False
        >>> expr(foo=42)
        True

        Per default the return value is converted to `None` if the
        expression returns an undefined value.  This can be changed
        by setting `undefined_to_none` to `False`.

        >>> env.compile_expression('var')() is None
        True
        >>> env.compile_expression('var', undefined_to_none=False)()
        Undefined

        .. versionadded:: 2.1
        """
        parser = Parser(self, source, state='variable')
        exc_info = None
        try:
            expr = parser.parse_expression()
            if not parser.stream.eos:
                raise TemplateSyntaxError('chunk after expression',
                                          parser.stream.current.lineno,
                                          None, None)
            expr.set_environment(self)
        except TemplateSyntaxError:
            exc_info = sys.exc_info()
        if exc_info is not None:
            self.handle_exception(exc_info, source_hint=source)
        body = [nodes.Assign(nodes.Name('result', 'store'), expr, lineno=1)]
        template = self.from_string(nodes.Template(body, lineno=1))
        return TemplateExpression(template, undefined_to_none)

    def compile_templates(self, target, extensions=None, filter_func=None,
                          zip='deflated', log_function=None,
                          ignore_errors=True, py_compile=False):
        """Compiles all the templates the loader can find, compiles them
        and stores them in `target`.  If `zip` is `None`, instead of in a
        zipfile, the templates will be will be stored in a directory.
        By default a deflate zip algorithm is used, to switch to
        the stored algorithm, `zip` can be set to ``'stored'``.

        `extensions` and `filter_func` are passed to :meth:`list_templates`.
        Each template returned will be compiled to the target folder or
        zipfile.

        By default template compilation errors are ignored.  In case a
        log function is provided, errors are logged.  If you want template
        syntax errors to abort the compilation you can set `ignore_errors`
        to `False` and you will get an exception on syntax errors.

        If `py_compile` is set to `True` .pyc files will be written to the
        target instead of standard .py files.

        .. versionadded:: 2.4
        """
        from ambari_jinja2.loaders import ModuleLoader

        if log_function is None:
            log_function = lambda x: None

        if py_compile:
            import imp, struct, marshal
            py_header = imp.get_magic() + \
                '\xff\xff\xff\xff'.encode('iso-8859-15')

        def write_file(filename, data, mode):
            if zip:
                info = ZipInfo(filename)
                info.external_attr = 0o755 << 16
                zip_file.writestr(info, data)
            else:
                f = open(os.path.join(target, filename), mode)
                try:
                    f.write(data)
                finally:
                    f.close()

        if zip is not None:
            from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED
            zip_file = ZipFile(target, 'w', dict(deflated=ZIP_DEFLATED,
                                                 stored=ZIP_STORED)[zip])
            log_function('Compiling into Zip archive "%s"' % target)
        else:
            if not os.path.isdir(target):
                os.makedirs(target)
            log_function('Compiling into folder "%s"' % target)

        try:
            for name in self.list_templates(extensions, filter_func):
                source, filename, _ = self.loader.get_source(self, name)
                try:
                    code = self.compile(source, name, filename, True, True)
                except TemplateSyntaxError as e:
                    if not ignore_errors:
                        raise
                    log_function('Could not compile "%s": %s' % (name, e))
                    continue

                filename = ModuleLoader.get_module_filename(name)

                if py_compile:
                    c = self._compile(code, _encode_filename(filename))
                    write_file(filename + 'c', py_header +
                               marshal.dumps(c), 'wb')
                    log_function('Byte-compiled "%s" as %s' %
                                 (name, filename + 'c'))
                else:
                    write_file(filename, code, 'w')
                    log_function('Compiled "%s" as %s' % (name, filename))
        finally:
            if zip:
                zip_file.close()

        log_function('Finished compiling templates')

    def list_templates(self, extensions=None, filter_func=None):
        """Returns a list of templates for this environment.  This requires
        that the loader supports the loader's
        :meth:`~BaseLoader.list_templates` method.

        If there are other files in the template folder besides the
        actual templates, the returned list can be filtered.  There are two
        ways: either `extensions` is set to a list of file extensions for
        templates, or a `filter_func` can be provided which is a callable that
        is passed a template name and should return `True` if it should end up
        in the result list.

        If the loader does not support that, a :exc:`TypeError` is raised.
        """
        x = self.loader.list_templates()
        if extensions is not None:
            if filter_func is not None:
                raise TypeError('either extensions or filter_func '
                                'can be passed, but not both')
            filter_func = lambda x: '.' in x and \
                                    x.rsplit('.', 1)[1] in extensions
        if filter_func is not None:
            x = filter(filter_func, x)
        return x

    def handle_exception(self, exc_info=None, rendered=False, source_hint=None):
        """Exception handling helper.  This is used internally to either raise
        rewritten exceptions or return a rendered traceback for the template.
        """
        global _make_traceback
        if exc_info is None:
            exc_info = sys.exc_info()

        # the debugging module is imported when it's used for the first time.
        # we're doing a lot of stuff there and for applications that do not
        # get any exceptions in template rendering there is no need to load
        # all of that.
        if _make_traceback is None:
            from ambari_jinja2.debug import make_traceback as _make_traceback
        traceback = _make_traceback(exc_info, source_hint)
        if rendered and self.exception_formatter is not None:
            return self.exception_formatter(traceback)
        if self.exception_handler is not None:
            self.exception_handler(traceback)
        exc_type, exc_value, tb = traceback.standard_exc_info
        raise exc_type(exc_value).with_traceback(tb)

    def join_path(self, template, parent):
        """Join a template with the parent.  By default all the lookups are
        relative to the loader root so this method returns the `template`
        parameter unchanged, but if the paths should be relative to the
        parent template, this function can be used to calculate the real
        template name.

        Subclasses may override this method and implement template path
        joining here.
        """
        return template

    @internalcode
    def _load_template(self, name, globals):
        if self.loader is None:
            raise TypeError('no loader for this environment specified')
        if self.cache is not None:
            template = self.cache.get(name)
            if template is not None and (not self.auto_reload or \
                                         template.is_up_to_date):
                return template
        template = self.loader.load(self, name, globals)
        if self.cache is not None:
            self.cache[name] = template
        return template

    @internalcode
    def get_template(self, name, parent=None, globals=None):
        """Load a template from the loader.  If a loader is configured this
        method ask the loader for the template and returns a :class:`Template`.
        If the `parent` parameter is not `None`, :meth:`join_path` is called
        to get the real template name before loading.

        The `globals` parameter can be used to provide template wide globals.
        These variables are available in the context at render time.

        If the template does not exist a :exc:`TemplateNotFound` exception is
        raised.

        .. versionchanged:: 2.4
           If `name` is a :class:`Template` object it is returned from the
           function unchanged.
        """
        if isinstance(name, Template):
            return name
        if parent is not None:
            name = self.join_path(name, parent)
        return self._load_template(name, self.make_globals(globals))

    @internalcode
    def select_template(self, names, parent=None, globals=None):
        """Works like :meth:`get_template` but tries a number of templates
        before it fails.  If it cannot find any of the templates, it will
        raise a :exc:`TemplatesNotFound` exception.

        .. versionadded:: 2.3

        .. versionchanged:: 2.4
           If `names` contains a :class:`Template` object it is returned
           from the function unchanged.
        """
        if not names:
            raise TemplatesNotFound(message='Tried to select from an empty list '
                                            'of templates.')
        globals = self.make_globals(globals)
        for name in names:
            if isinstance(name, Template):
                return name
            if parent is not None:
                name = self.join_path(name, parent)
            try:
                return self._load_template(name, globals)
            except TemplateNotFound:
                pass
        raise TemplatesNotFound(names)

    @internalcode
    def get_or_select_template(self, template_name_or_list,
                               parent=None, globals=None):
        """Does a typecheck and dispatches to :meth:`select_template`
        if an iterable of template names is given, otherwise to
        :meth:`get_template`.

        .. versionadded:: 2.3
        """
        if isinstance(template_name_or_list, str):
            return self.get_template(template_name_or_list, parent, globals)
        elif isinstance(template_name_or_list, Template):
            return template_name_or_list
        return self.select_template(template_name_or_list, parent, globals)

    def from_string(self, source, globals=None, template_class=None):
        """Load a template from a string.  This parses the source given and
        returns a :class:`Template` object.
        """
        globals = self.make_globals(globals)
        cls = template_class or self.template_class
        return cls.from_code(self, self.compile(source), globals, None)

    def make_globals(self, d):
        """Return a dict for the globals."""
        if not d:
            return self.globals
        return dict(self.globals, **d)


class Template(object):
    """The central template object.  This class represents a compiled template
    and is used to evaluate it.

    Normally the template object is generated from an :class:`Environment` but
    it also has a constructor that makes it possible to create a template
    instance directly using the constructor.  It takes the same arguments as
    the environment constructor but it's not possible to specify a loader.

    Every template object has a few methods and members that are guaranteed
    to exist.  However it's important that a template object should be
    considered immutable.  Modifications on the object are not supported.

    Template objects created from the constructor rather than an environment
    do have an `environment` attribute that points to a temporary environment
    that is probably shared with other templates created with the constructor
    and compatible settings.

    >>> template = Template('Hello {{ name }}!')
    >>> template.render(name='John Doe')
    u'Hello John Doe!'

    >>> stream = template.stream(name='John Doe')
    >>> stream.next()
    u'Hello John Doe!'
    >>> stream.next()
    Traceback (most recent call last):
        ...
    StopIteration
    """

    def __new__(cls, source,
                block_start_string=BLOCK_START_STRING,
                block_end_string=BLOCK_END_STRING,
                variable_start_string=VARIABLE_START_STRING,
                variable_end_string=VARIABLE_END_STRING,
                comment_start_string=COMMENT_START_STRING,
                comment_end_string=COMMENT_END_STRING,
                line_statement_prefix=LINE_STATEMENT_PREFIX,
                line_comment_prefix=LINE_COMMENT_PREFIX,
                trim_blocks=TRIM_BLOCKS,
                newline_sequence=NEWLINE_SEQUENCE,
                extensions=(),
                optimized=True,
                undefined=Undefined,
                finalize=None,
                autoescape=False):
        env = get_spontaneous_environment(
            block_start_string, block_end_string, variable_start_string,
            variable_end_string, comment_start_string, comment_end_string,
            line_statement_prefix, line_comment_prefix, trim_blocks,
            newline_sequence, frozenset(extensions), optimized, undefined,
            finalize, autoescape, None, 0, False, None)
        return env.from_string(source, template_class=cls)

    @classmethod
    def from_code(cls, environment, code, globals, uptodate=None):
        """Creates a template object from compiled code and the globals.  This
        is used by the loaders and environment to create a template object.
        """
        namespace = {
            'environment':  environment,
            '__file__':     code.co_filename
        }
        exec(code, namespace)
        rv = cls._from_namespace(environment, namespace, globals)
        rv._uptodate = uptodate
        return rv

    @classmethod
    def from_module_dict(cls, environment, module_dict, globals):
        """Creates a template object from a module.  This is used by the
        module loader to create a template object.

        .. versionadded:: 2.4
        """
        return cls._from_namespace(environment, module_dict, globals)

    @classmethod
    def _from_namespace(cls, environment, namespace, globals):
        t = object.__new__(cls)
        t.environment = environment
        t.globals = globals
        t.name = namespace['name']
        t.filename = namespace['__file__']
        t.blocks = namespace['blocks']

        # render function and module
        t.root_render_func = namespace['root']
        t._module = None

        # debug and loader helpers
        t._debug_info = namespace['debug_info']
        t._uptodate = None

        # store the reference
        namespace['environment'] = environment
        namespace['__jinja_template__'] = t

        return t

    def render(self, *args, **kwargs):
        """This method accepts the same arguments as the `dict` constructor:
        A dict, a dict subclass or some keyword arguments.  If no arguments
        are given the context will be empty.  These two calls do the same::

            template.render(knights='that say nih')
            template.render({'knights': 'that say nih'})

        This will return the rendered template as unicode string.
        """
        vars = dict(*args, **kwargs)
        try:
            return concat(self.root_render_func(self.new_context(vars)))
        except:
            exc_info = sys.exc_info()
        return self.environment.handle_exception(exc_info, True)

    def stream(self, *args, **kwargs):
        """Works exactly like :meth:`generate` but returns a
        :class:`TemplateStream`.
        """
        return TemplateStream(self.generate(*args, **kwargs))

    def generate(self, *args, **kwargs):
        """For very large templates it can be useful to not render the whole
        template at once but evaluate each statement after another and yield
        piece for piece.  This method basically does exactly that and returns
        a generator that yields one item after another as unicode strings.

        It accepts the same arguments as :meth:`render`.
        """
        vars = dict(*args, **kwargs)
        try:
            for event in self.root_render_func(self.new_context(vars)):
                yield event
        except:
            exc_info = sys.exc_info()
        else:
            return
        yield self.environment.handle_exception(exc_info, True)

    def new_context(self, vars=None, shared=False, locals=None):
        """Create a new :class:`Context` for this template.  The vars
        provided will be passed to the template.  Per default the globals
        are added to the context.  If shared is set to `True` the data
        is passed as it to the context without adding the globals.

        `locals` can be a dict of local variables for internal usage.
        """
        return new_context(self.environment, self.name, self.blocks,
                           vars, shared, self.globals, locals)

    def make_module(self, vars=None, shared=False, locals=None):
        """This method works like the :attr:`module` attribute when called
        without arguments but it will evaluate the template on every call
        rather than caching it.  It's also possible to provide
        a dict which is then used as context.  The arguments are the same
        as for the :meth:`new_context` method.
        """
        return TemplateModule(self, self.new_context(vars, shared, locals))

    @property
    def module(self):
        """The template as module.  This is used for imports in the
        template runtime but is also useful if one wants to access
        exported template variables from the Python layer:

        >>> t = Template('{% macro foo() %}42{% endmacro %}23')
        >>> unicode(t.module)
        u'23'
        >>> t.module.foo()
        u'42'
        """
        if self._module is not None:
            return self._module
        self._module = rv = self.make_module()
        return rv

    def get_corresponding_lineno(self, lineno):
        """Return the source line number of a line number in the
        generated bytecode as they are not in sync.
        """
        for template_line, code_line in reversed(self.debug_info):
            if code_line <= lineno:
                return template_line
        return 1

    @property
    def is_up_to_date(self):
        """If this variable is `False` there is a newer version available."""
        if self._uptodate is None:
            return True
        return self._uptodate()

    @property
    def debug_info(self):
        """The debug info mapping."""
        return [tuple(map(int, x.split('='))) for x in
                self._debug_info.split('&')]

    def __repr__(self):
        if self.name is None:
            name = 'memory:%x' % id(self)
        else:
            name = repr(self.name)
        return '<%s %s>' % (self.__class__.__name__, name)


class TemplateModule(object):
    """Represents an imported template.  All the exported names of the
    template are available as attributes on this object.  Additionally
    converting it into an unicode- or bytestrings renders the contents.
    """

    def __init__(self, template, context):
        self._body_stream = list(template.root_render_func(context))
        self.__dict__.update(context.get_exported())
        self.__name__ = template.name

    def __html__(self):
        return Markup(concat(self._body_stream))

    def __str__(self):
        return concat(self._body_stream)

    def __repr__(self):
        if self.__name__ is None:
            name = 'memory:%x' % id(self)
        else:
            name = repr(self.__name__)
        return '<%s %s>' % (self.__class__.__name__, name)


class TemplateExpression(object):
    """The :meth:`ambari_jinja2.Environment.compile_expression` method returns an
    instance of this object.  It encapsulates the expression-like access
    to the template with an expression it wraps.
    """

    def __init__(self, template, undefined_to_none):
        self._template = template
        self._undefined_to_none = undefined_to_none

    def __call__(self, *args, **kwargs):
        context = self._template.new_context(dict(*args, **kwargs))
        consume(self._template.root_render_func(context))
        rv = context.vars['result']
        if self._undefined_to_none and isinstance(rv, Undefined):
            rv = None
        return rv


class TemplateStream(object):
    """A template stream works pretty much like an ordinary python generator
    but it can buffer multiple items to reduce the number of total iterations.
    Per default the output is unbuffered which means that for every unbuffered
    instruction in the template one unicode string is yielded.

    If buffering is enabled with a buffer size of 5, five items are combined
    into a new unicode string.  This is mainly useful if you are streaming
    big templates to a client via WSGI which flushes after each iteration.
    """

    def __init__(self, gen):
        self._gen = gen
        self.disable_buffering()

    def dump(self, fp, encoding=None, errors='strict'):
        """Dump the complete stream into a file or file-like object.
        Per default unicode strings are written, if you want to encode
        before writing specifiy an `encoding`.

        Example usage::

            Template('Hello {{ name }}!').stream(name='foo').dump('hello.html')
        """
        close = False
        if isinstance(fp, str):
            fp = file(fp, 'w')
            close = True
        try:
            if encoding is not None:
                iterable = (x.encode(encoding, errors) for x in self)
            else:
                iterable = self
            if hasattr(fp, 'writelines'):
                fp.writelines(iterable)
            else:
                for item in iterable:
                    fp.write(item)
        finally:
            if close:
                fp.close()

    def disable_buffering(self):
        """Disable the output buffering."""
        self._next = self._gen.__next__
        self.buffered = False

    def enable_buffering(self, size=5):
        """Enable buffering.  Buffer `size` items before yielding them."""
        if size <= 1:
            raise ValueError('buffer size too small')

        def generator(next):
            buf = []
            c_size = 0
            push = buf.append

            while 1:
                try:
                    while c_size < size:
                        c = next()
                        push(c)
                        if c:
                            c_size += 1
                except StopIteration:
                    if not c_size:
                        return
                yield concat(buf)
                del buf[:]
                c_size = 0

        self.buffered = True
        self._next = generator(self._gen.__next__).__next__

    def __iter__(self):
        return self

    def __next__(self):
        return self._next()


# hook in default template class.  if anyone reads this comment: ignore that
# it's possible to use custom templates ;-)
Environment.template_class = Template
