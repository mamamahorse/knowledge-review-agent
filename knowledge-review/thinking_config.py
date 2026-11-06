"""DeepSeek-specific request options; do not alter other compatible providers."""
import os
from urllib.parse import urlsplit


def thinking_options(client):
    if urlsplit(str(client.base_url)).hostname != 'api.deepseek.com':
        return {}, False
    mode = os.getenv('LLM_THINKING', 'enabled').strip().lower()
    effort = os.getenv('LLM_REASONING_EFFORT', 'high').strip().lower()
    if mode not in ('enabled', 'disabled'):
        raise ValueError('LLM_THINKING must be enabled or disabled')
    if effort not in ('low', 'high', 'max'):
        raise ValueError('LLM_REASONING_EFFORT must be low, high or max')
    options = {'extra_body': {'thinking': {'type': mode}}}
    if mode == 'enabled': options['reasoning_effort'] = effort
    return options, mode == 'enabled'
