"""
pytest PluginManager, basic initialization and tracing.
"""
import os
import sys
import inspect
import py
# don't import pytest to avoid circular imports

assert py.__version__.split(".")[:2] >= ['1', '4'], ("installation problem: "
    "%s is too old, remove or upgrade 'py'" % (py.__version__))

class TagTracer:
    def __init__(self):
        self._tag2proc = {}
        self.writer = None
        self.indent = 0

    def get(self, name):
        return TagTracerSub(self, (name,))

    def format_message(self, tags, args):
        if isinstance(args[-1], dict):
            extra = args[-1]
            args = args[:-1]
        else:
            extra = {}

        content = " ".join(map(str, args))
        indent = "  " * self.indent

        lines = [
            "%s%s [%s]\n" %(indent, content, ":".join(tags))
        ]

        for name, value in extra.items():
            lines.append("%s    %s: %s\n" % (indent, name, value))
        return lines

    def processmessage(self, tags, args):
        if self.writer is not None and args:
            lines = self.format_message(tags, args)
            self.writer(''.join(lines))
        try:
            self._tag2proc[tags](tags, args)
        except KeyError:
            pass

    def setwriter(self, writer):
        self.writer = writer

    def setprocessor(self, tags, processor):
        if isinstance(tags, str):
            tags = tuple(tags.split(":"))
        else:
            assert isinstance(tags, tuple)
        self._tag2proc[tags] = processor

class TagTracerSub:
    def __init__(self, root, tags):
        self.root = root
        self.tags = tags
    def __call__(self, *args):
        self.root.processmessage(self.tags, args)
    def setmyprocessor(self, processor):
        self.root.setprocessor(self.tags, processor)
    def get(self, name):
        return self.__class__(self.root, self.tags + (name,))

class PluginManager(object):
    def __init__(self, hookspecs=None, prefix="pytest_"):
        self._name2plugin = {}
        self._listattrcache = {}
        self._plugins = []
        self._conftestplugins = []
        self._warnings = []
        self.trace = TagTracer().get("pluginmanage")
        self._plugin_distinfo = []
        self._shutdown = []
        self.hook = HookRelay(hookspecs or [], pm=self, prefix=prefix)

    def do_configure(self, config):
        # backward compatibility
        config.do_configure()

    def set_register_callback(self, callback):
        assert not hasattr(self, "_registercallback")
        self._registercallback = callback

    def register(self, plugin, name=None, prepend=False, conftest=False):
        if self._name2plugin.get(name, None) == -1:
            return
        name = name or getattr(plugin, '__name__', str(id(plugin)))
        if self.isregistered(plugin, name):
            raise ValueError("Plugin already registered: %s=%s\n%s" %(
                              name, plugin, self._name2plugin))
        #self.trace("registering", name, plugin)
        reg = getattr(self, "_registercallback", None)
        if reg is not None:
            reg(plugin, name)  # may call addhooks
        self.hook._scan_plugin(plugin)
        self._name2plugin[name] = plugin
        if conftest:
            self._conftestplugins.append(plugin)
        else:
            if not prepend:
                self._plugins.append(plugin)
            else:
                self._plugins.insert(0, plugin)
        return True

    def unregister(self, plugin=None, name=None):
        if plugin is None:
            plugin = self.getplugin(name=name)
        try:
            self._plugins.remove(plugin)
        except KeyError:
            self._conftestplugins.remove(plugin)
        for name, value in list(self._name2plugin.items()):
            if value == plugin:
                del self._name2plugin[name]

    def add_shutdown(self, func):
        self._shutdown.append(func)

    def ensure_shutdown(self):
        while self._shutdown:
            func = self._shutdown.pop()
            func()
        self._plugins = self._conftestplugins = []
        self._name2plugin.clear()
        self._listattrcache.clear()

    def isregistered(self, plugin, name=None):
        if self.getplugin(name) is not None:
            return True
        for val in self._name2plugin.values():
            if plugin == val:
                return True

    def addhooks(self, spec, prefix="pytest_"):
        self.hook._addhooks(spec, prefix=prefix)

    def getplugins(self):
        return self._plugins + self._conftestplugins

    def skipifmissing(self, name):
        if not self.hasplugin(name):
            import pytest
            pytest.skip("plugin %r is missing" % name)

    def hasplugin(self, name):
        return bool(self.getplugin(name))

    def getplugin(self, name):
        if name is None:
            return None
        try:
            return self._name2plugin[name]
        except KeyError:
            return self._name2plugin.get("_pytest." + name, None)

    # API for bootstrapping
    #
    def _envlist(self, varname):
        val = os.environ.get(varname, None)
        if val is not None:
            return val.split(',')
        return ()

    def consider_env(self):
        for spec in self._envlist("PYTEST_PLUGINS"):
            self.import_plugin(spec)

    def consider_setuptools_entrypoints(self):
        try:
            from pkg_resources import iter_entry_points, DistributionNotFound
        except ImportError:
            return # XXX issue a warning
        for ep in iter_entry_points('pytest11'):
            name = ep.name
            if name.startswith("pytest_"):
                name = name[7:]
            if ep.name in self._name2plugin or name in self._name2plugin:
                continue
            try:
                plugin = ep.load()
            except DistributionNotFound:
                continue
            self._plugin_distinfo.append((ep.dist, plugin))
            self.register(plugin, name=name)

    def consider_preparse(self, args):
        for opt1,opt2 in zip(args, args[1:]):
            if opt1 == "-p":
                self.consider_pluginarg(opt2)

    def consider_pluginarg(self, arg):
        if arg.startswith("no:"):
            name = arg[3:]
            if self.getplugin(name) is not None:
                self.unregister(None, name=name)
            self._name2plugin[name] = -1
        else:
            if self.getplugin(arg) is None:
                self.import_plugin(arg)

    def consider_conftest(self, conftestmodule):
        if self.register(conftestmodule, name=conftestmodule.__file__,
                         conftest=True):
            self.consider_module(conftestmodule)

    def consider_module(self, mod):
        attr = getattr(mod, "pytest_plugins", ())
        if attr:
            if not isinstance(attr, (list, tuple)):
                attr = (attr,)
            for spec in attr:
                self.import_plugin(spec)

    def import_plugin(self, modname):
        assert isinstance(modname, str)
        if self.getplugin(modname) is not None:
            return
        try:
            mod = importplugin(modname)
        except KeyboardInterrupt:
            raise
        except ImportError:
            if modname.startswith("pytest_"):
                return self.import_plugin(modname[7:])
            raise
        except:
            e = sys.exc_info()[1]
            import pytest
            if not hasattr(pytest, 'skip') or not isinstance(e, pytest.skip.Exception):
                raise
            self._warnings.append("skipped plugin %r: %s" %((modname, e.msg)))
        else:
            self.register(mod, modname)
            self.consider_module(mod)

    def listattr(self, attrname, plugins=None):
        if plugins is None:
            plugins = self._plugins + self._conftestplugins
        l = []
        last = []
        wrappers = []
        for plugin in plugins:
            try:
                meth = getattr(plugin, attrname)
            except AttributeError:
                continue
            if hasattr(meth, 'hookwrapper'):
                wrappers.append(meth)
            elif hasattr(meth, 'tryfirst'):
                last.append(meth)
            elif hasattr(meth, 'trylast'):
                l.insert(0, meth)
            else:
                l.append(meth)
        l.extend(last)
        l.extend(wrappers)
        #self._listattrcache[key] = list(l)
        return l

    def call_plugin(self, plugin, methname, kwargs):
        return MultiCall(methods=self.listattr(methname, plugins=[plugin]),
                kwargs=kwargs, firstresult=True).execute()


def importplugin(importspec):
    name = importspec
    try:
        mod = "_pytest." + name
        __import__(mod)
        return sys.modules[mod]
    except ImportError:
        __import__(importspec)
        return sys.modules[importspec]

class MultiCall:
    """ execute a call into multiple python functions/methods. """

    class WrongHookWrapper(Exception):
        """ a hook wrapper does not behave correctly. """
        def __init__(self, func, message):
            Exception.__init__(self, func, message)
            self.func = func
            self.message = message

    def __init__(self, methods, kwargs, firstresult=False):
        self.methods = list(methods)
        self.kwargs = kwargs
        self.results = []
        self.firstresult = firstresult

    def __repr__(self):
        status = "%d results, %d meths" % (len(self.results), len(self.methods))
        return "<MultiCall %s, kwargs=%r>" %(status, self.kwargs)

    def execute(self):
        next_finalizers = []
        try:
            while self.methods:
                method = self.methods.pop()
                kwargs = self.getkwargs(method)
                if hasattr(method, "hookwrapper"):
                    it = method(**kwargs)
                    next = getattr(it, "next", None)
                    if next is None:
                        next = getattr(it, "__next__", None)
                        if next is None:
                            raise self.WrongHookWrapper(method,
                                "wrapper does not contain a yield")
                    res = next()
                    next_finalizers.append((method, next))
                else:
                    res = method(**kwargs)
                if res is not None:
                    self.results.append(res)
                    if self.firstresult:
                        return res
            if not self.firstresult:
                return self.results
        finally:
            for method, fin in reversed(next_finalizers):
                try:
                    fin()
                except StopIteration:
                    pass
                else:
                    raise self.WrongHookWrapper(method,
                                "wrapper contain more than one yield")


    def getkwargs(self, method):
        kwargs = {}
        for argname in varnames(method):
            try:
                kwargs[argname] = self.kwargs[argname]
            except KeyError:
                if argname == "__multicall__":
                    kwargs[argname] = self
        return kwargs

def varnames(func):
    """ return argument name tuple for a function, method, class or callable.

    In case of a class, its "__init__" method is considered.
    For methods the "self" parameter is not included unless you are passing
    an unbound method with Python3 (which has no supports for unbound methods)
    """
    cache = getattr(func, "__dict__", {})
    try:
        return cache["_varnames"]
    except KeyError:
        pass
    if inspect.isclass(func):
        try:
            func = func.__init__
        except AttributeError:
            return ()
        ismethod = True
    else:
        if not inspect.isfunction(func) and not inspect.ismethod(func):
            func = getattr(func, '__call__', func)
        ismethod = inspect.ismethod(func)
    rawcode = py.code.getrawcode(func)
    try:
        x = rawcode.co_varnames[ismethod:rawcode.co_argcount]
    except AttributeError:
        x = ()
    try:
        cache["_varnames"] = x
    except TypeError:
        pass
    return x

class HookRelay:
    def __init__(self, hookspecs, pm, prefix="pytest_"):
        if not isinstance(hookspecs, list):
            hookspecs = [hookspecs]
        self._hookspecs = []
        self._pm = pm
        self.trace = pm.trace.root.get("hook")
        self.prefix = prefix
        for hookspec in hookspecs:
            self._addhooks(hookspec, prefix)

    def _addhooks(self, hookspec, prefix):
        self._hookspecs.append(hookspec)
        added = False
        for name in dir(hookspec):
            if name.startswith(prefix):
                method = getattr(hookspec, name)
                firstresult = getattr(method, 'firstresult', False)
                hc = HookCaller(self, name, firstresult=firstresult,
                                argnames=varnames(method))
                setattr(self, name, hc)
                added = True
                #print ("setting new hook", name)
        if not added:
            raise ValueError("did not find new %r hooks in %r" %(
                prefix, hookspec,))

    def _getcaller(self, name, plugins):
        caller = getattr(self, name)
        methods = self._pm.listattr(name, plugins=plugins)
        if methods:
            return caller.new_cached_caller(methods)
        return caller

    def _scan_plugin(self, plugin):
        def fail(msg, *args):
            name = getattr(plugin, '__name__', plugin)
            raise PluginValidationError("plugin %r\n%s" %(name, msg % args))

        for name in dir(plugin):
            if not name.startswith(self.prefix):
                continue
            hook = getattr(self, name, None)
            method = getattr(plugin, name)
            if hook is None:
                is_optional = getattr(method, 'optionalhook', False)
                if not isgenerichook(name) and not is_optional:
                    fail("found unknown hook: %r", name)
                continue
            for arg in varnames(method):
                if arg not in hook.argnames:
                    fail("argument %r not available\n"
                         "actual definition: %s\n"
                         "available hookargs: %s",
                         arg, formatdef(method),
                           ", ".join(hook.argnames))
            getattr(self, name).clear_method_cache()


class HookCaller:
    def __init__(self, hookrelay, name, firstresult, argnames, methods=None):
        self.hookrelay = hookrelay
        self.name = name
        self.firstresult = firstresult
        self.trace = self.hookrelay.trace
        self.methods = methods
        self.argnames = ["__multicall__"]
        self.argnames.extend(argnames)
        assert "self" not in argnames

    def new_cached_caller(self, methods):
        return HookCaller(self.hookrelay, self.name, self.firstresult,
                          argnames=self.argnames, methods=methods)

    def __repr__(self):
        return "<HookCaller %r>" %(self.name,)

    def clear_method_cache(self):
        self.methods = None

    def __call__(self, **kwargs):
        methods = self.methods
        if methods is None:
            self.methods = methods = self.hookrelay._pm.listattr(self.name)
        return self._docall(methods, kwargs)

    def callextra(self, methods, **kwargs):
        #if self.methods is None:
        #    self.reload_methods()
        return self._docall(self.methods + methods, kwargs)

    def _docall(self, methods, kwargs):
        self.trace(self.name, kwargs)
        self.trace.root.indent += 1
        mc = MultiCall(methods, kwargs, firstresult=self.firstresult)
        try:
            res = mc.execute()
            if res:
                self.trace("finish", self.name, "-->", res)
        finally:
            self.trace.root.indent -= 1
        return res



class PluginValidationError(Exception):
    """ plugin failed validation. """

def isgenerichook(name):
    return name == "pytest_plugins" or \
           name.startswith("pytest_funcarg__")

def collectattr(obj):
    methods = {}
    for apiname in dir(obj):
        if apiname.startswith("pytest_"):
            methods[apiname] = getattr(obj, apiname)
    return methods

def formatdef(func):
    return "%s%s" % (
        func.__name__,
        inspect.formatargspec(*inspect.getargspec(func))
    )

