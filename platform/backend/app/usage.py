"""Aggregate provider-reported tokens without estimating missing measurements."""
import math


def safe_usage(value):
    value = value if isinstance(value, dict) else {}
    result = {}
    for key in ('prompt_tokens', 'completion_tokens'):
        count = value.get(key)
        result[key] = count if type(count) is int and count >= 0 else None
    seconds = value.get('generation_seconds')
    result['generation_seconds'] = (seconds if type(seconds) in (int, float)
        and math.isfinite(seconds) and seconds >= 0 else None)
    for key in ('request_seconds','gateway_seconds','queue_wait_seconds','prompt_prepare_seconds','model_load_seconds',
                'tokenize_seconds','model_generate_seconds','input_prepare_seconds','time_to_first_token_seconds','decode_seconds','json_validation_seconds',
                'network_residual_seconds'):
        number=value.get(key)
        if type(number) in (int,float) and math.isfinite(number) and number>=0:result[key]=number
    for key,fields in [('gpu_memory',('device','total_bytes','free_bytes','peak_allocated_bytes','peak_reserved_bytes')),('gpu_utilization',('snapshot_percent',))]:
        if isinstance(value.get(key),dict):
            result[key]={name:number for name,number in value[key].items() if name in fields and type(number) in (int,float) and math.isfinite(number) and number>=0}
    if isinstance(value.get('control_ids'),list):result['control_ids']=[x for x in value['control_ids'] if isinstance(x,str)]
    return result


def process_usage(provider, wall_seconds):
    delegate = getattr(provider, 'delegate', provider)
    calls = getattr(delegate, 'usage_calls', [])
    offline = provider.name.startswith(('offline', 'checklist:'))
    def total(key):
        if offline:
            return 0
        values = [call[key] for call in calls]
        return sum(values) if values and all(value is not None for value in values) else None
    prompt, completion = total('prompt_tokens'), total('completion_tokens')
    measured = [call for call in calls if call['prompt_tokens'] is not None and call['completion_tokens'] is not None]
    return {'provider': provider.name, 'requests': len(calls),
            'prompt_tokens': prompt, 'completion_tokens': completion,
            'total_tokens': prompt + completion if prompt is not None and completion is not None else None,
            'generation_seconds': total('generation_seconds'), 'wall_seconds': round(wall_seconds, 3),
            'measured_total_tokens': sum(call['prompt_tokens'] + call['completion_tokens'] for call in measured),
            'unmeasured_requests': len(calls) - len(measured),
            'calls': calls}


class UsageLedger(list):
    """Account for each worker's call independently, including adapter extensions."""
    def __init__(self,lock,thread_state):
        super().__init__()
        self.lock=lock
        self.thread_state=thread_state

    def append(self,value):
        with self.lock:super().append(value)
        self.thread_state.recorded=True
