from typing import Any, Optional

import torch

from pytorch_pfn_extras import reporting
from pytorch_pfn_extras.training import extension
from pytorch_pfn_extras.training import trigger as trigger_module
from pytorch_pfn_extras.training._manager_protocol import ExtensionsManagerProtocol


_default_statistics = {
    'mean': lambda x: torch.mean(x),
    'std': lambda x: torch.std(x),
    'min': lambda x: torch.min(x),
    'max': lambda x: torch.max(x),
    'zeros': lambda x: (x == 0).sum(),
    # 'percentile': lambda x: backend.get_array_module(x).percentile(
    #     x, (0.13, 2.28, 15.87, 50, 84.13, 97.72, 99.87))
}


class ParameterStatistics(extension.Extension):
    """An extension to report parameter statistics.

    Statistics are collected and reported for a given :class:`~torch.nn.Module`
    or an iterable of :class:`~torch.nn.Module`\\ s. If a link contains child
    modules, the statistics are reported separately for each child.

    Any function that takes a one-dimensional :class:`torch.Tensor`
    and outputs a single or multiple real numbers can be registered to
    handle the collection of statistics, e.g.
    :meth:`numpy.ndarray.mean`.

    The keys of reported statistics follow the convention of link name
    followed by parameter name, attribute name and function name, e.g.
    ``VGG16Layers/conv1_1/W/data/mean``. They are prepended with an optional
    prefix and appended with integer indices if the statistics generating
    function return multiple values.

    Args:
        links (instance or iterable of ~torch.nn.Module): Module(s) containing
            the parameters to observe. The link is expected to have a ``name``
            attribute which is used as a part of the report key.
        statistics (dict or 'default'): Dictionary with function name to
            function mappings.
            The name is a string and is used as a part of the report key. The
            function is responsible for generating the statistics.
            If the special value ``'default'`` is specified, the default
            statistics functions will be used.
        report_params (bool): If ``True``, report statistics for parameter
            values such as weights and biases.
        report_grads (bool): If ``True``, report statistics for parameter
            gradients.
        prefix (str): Optional prefix to prepend to the report keys.
        trigger: Trigger that decides when to aggregate the results and report
            the values.
        skip_nan_params (bool): If ``True``, statistics are not computed for
            parameters including NaNs and a single NaN value is immediately
            reported instead. Otherwise, this extension will simply try to
            compute the statistics without performing any checks for NaNs.

    .. note::

       The default statistic functions are as follows:

       * ``'mean'`` (``xp.mean(x)``)
       * ``'std'`` (``xp.std(x)``)
       * ``'min'`` (``xp.min(x)``)
       * ``'max'`` (``xp.max(x)``)
       * ``'zeros'`` (``xp.count_nonzero(x == 0)``)
       * ``'percentile'`` (``xp.percentile(x, \
(0.13, 2.28, 15.87, 50, 84.13, 97.72, 99.87))``)

    """
    default_name = 'parameter_statistics'
    priority = extension.PRIORITY_WRITER

    # prefix ends with a '/' and param_name is preceded by a '/'
    report_key_template = ('{prefix}{param_name}/{attr_name}/'
                           '{function_name}')

    default_statistics = _default_statistics

    def __init__(
            self,
            links: Any,
            statistics: Any = 'default',
            report_params: bool = True,
            report_grads: bool = True,
            prefix: Optional[str] = None,
            trigger: trigger_module.TriggerLike = (1, 'epoch'),
            skip_nan_params: bool = False,
    ):

        if not isinstance(links, (list, tuple)):
            links = links,
        self._links = links

        if statistics is None:
            statistics = {}
        elif statistics == 'default':
            statistics = self.default_statistics
        self._statistics = dict(statistics)

        attrs = []
        if report_params:
            attrs.append('data')
        if report_grads:
            attrs.append('grad')
        self._attrs = attrs

        self._prefix = prefix
        self._trigger = trigger_module.get_trigger(trigger)
        self._summary = reporting.DictSummary()
        self._skip_nan_params = skip_nan_params

    def __call__(self, manager: ExtensionsManagerProtocol) -> None:
        """Execute the statistics extension.

        Collect statistics for the current state of parameters.

        Note that this method will merely update its statistic summary, unless
        the internal trigger is fired. If the trigger is fired, the summary
        will also be reported and then reset for the next accumulation.

        Args:
            manager (~pytorch_pfn_extras.training.ExtensionsManager):
                Associated manager that invoked this extension.
        """
        statistics = {}

        for link in self._links:
            for param_name, param in link.named_parameters():
                for attr_name in self._attrs:
                    for function_name, function in self._statistics.items():
                        # Get parameters as a flattened one-dimensional array
                        # since the statistics function should make no
                        # assumption about the axes
                        params = getattr(param, attr_name).flatten()
                        if (self._skip_nan_params
                            and (
                                torch.isnan(params).any())):
                            value: Any = float('nan')
                        else:
                            value = function(params)
                        key = self.report_key_template.format(
                            prefix=self._prefix + '/' if self._prefix else '',
                            param_name=param_name,
                            attr_name=attr_name,
                            function_name=function_name
                        )
                        if (isinstance(value, torch.Tensor)
                                and value.numel() > 1):
                            # Append integer indices to the keys if the
                            # statistic function return multiple values
                            statistics.update({'{}/{}'.format(key, i): v for
                                               i, v in enumerate(value)})
                        else:
                            statistics[key] = value

        self._summary.add(statistics)

        if self._trigger(manager):
            reporting.report(self._summary.compute_mean())
            self._summary = reporting.DictSummary()  # Clear summary

    def register_statistics(self, name: str, function: Any) -> None:
        """Register a function to compute a certain statistic.

        The registered function will be called each time the extension runs and
        the results will be included in the report.

        Args:
            name (str): Name of the statistic.
            function: Function to generate the statistic. Any function that
                takes a one-dimensional :class:`numpy.ndarray` or a
                :class:`cupy.ndarray` and outputs a single or multiple real
                numbers is allowed.
        """
        self._statistics[name] = function
