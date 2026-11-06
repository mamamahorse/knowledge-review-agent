"""Shared tool contracts and independent recovery budgets; no business state machine."""
from openai import APIConnectionError, APITimeoutError, APIStatusError
import json


class ToolDenied(ValueError):
    pass


class ToolArgumentsError(ValueError):
    def __init__(self, issues):
        super().__init__('Tool arguments do not match the declared schema.')
        self.issues=issues


def tool_error(code, issues, message, *, retryable=True):
    return dict(code=code,retryable=retryable,issues=issues,message=message)


def validate_arguments(value, schema):
    issues=arguments_errors(value,schema)
    if issues: raise ToolArgumentsError(issues)


def parse_arguments(text):
    try: return json.loads(text)
    except (ValueError,TypeError) as exc:
        raise ToolArgumentsError([dict(path=[],code='invalid_json',message='Tool arguments must be valid JSON')]) from exc


def transient_transport_error(exc):
    return isinstance(exc,(APIConnectionError,APITimeoutError)) or (
        isinstance(exc,APIStatusError) and exc.status_code in (408,500,502,503,504))


class ToolRecovery:
    """State is caller-owned JSON. Resolve model errors once per complete tool batch.

    A semantic correction is consumed only on an actual model response. Transport
    retransmissions have their own persistent allowance and never clear tool errors.
    """
    LIMIT=2

    def __init__(self,state):
        self.state=state
        # Old jobs used a single slot for both kinds. Preserve the observed count;
        # historical mixed attempts cannot be reconstructed.
        old=state.get('correction')
        if old and old['error']['code']=='MODEL_TRANSPORT':
            if not state.get('transport'): state['transport']=old
            state['correction']=None

    def reset(self):
        self.state.update(correction=None,transport=None)

    def exhausted(self):
        return next((kind for kind in ('correction','transport')
            if self.state.get(kind) and self.state[kind]['attempts']>=self.LIMIT),None)

    def before_request(self):
        if self.state.get('transport'): self.state['transport']['attempts']+=1

    def response_received(self):
        self.transport_succeeded()
        if self.state.get('correction'): self.state['correction']['attempts']+=1

    def transport_succeeded(self):
        self.state['transport']=None

    def transport_failed(self,exc):
        old=self.state.get('transport')
        value=dict(attempts=old['attempts'] if old else 0,error_type=type(exc).__name__,
            http_status=getattr(exc,'status_code',None))
        self.state['transport']=value
        return dict(value)

    def finish_batch(self,errors):
        old=self.state.get('correction')
        if not errors:
            self.state['correction']=None
            return
        first=errors[0]
        self.state['correction']=dict(attempts=old['attempts'] if old else 0,
            errors=list(errors),**first)


def arguments_errors(value, schema, path=()):
    """Validate the JSON-schema vocabulary used by the project's tool registry."""
    kind=schema.get('type')
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'integer':type(value) is int,'number':type(value) in (int,float),'boolean':type(value) is bool}
    if kind and not valid.get(kind,False):
        return [dict(path=list(path),code='type',message=f'Expected {kind}')]
    issues=[]
    if kind=='object':
        props=schema.get('properties',{})
        issues.extend(dict(path=[*path,k],code='missing',message='Required field missing') for k in schema.get('required',[]) if k not in value)
        for key,item in value.items():
            if key not in props and schema.get('additionalProperties') is False:
                issues.append(dict(path=[*path,key],code='extra',message='Unknown field'))
            elif key in props: issues.extend(arguments_errors(item,props[key],(*path,key)))
    if 'enum' in schema and value not in schema['enum']: issues.append(dict(path=list(path),code='enum',message='Value is not an allowed choice'))
    for key,failed in [('minimum',lambda n:value<n),('maximum',lambda n:value>n),
                       ('minLength',lambda n:len(value)<n),('maxLength',lambda n:len(value)>n),
                       ('minItems',lambda n:len(value)<n),('maxItems',lambda n:len(value)>n)]:
        if key in schema and failed(schema[key]): issues.append(dict(path=list(path),code=key,message=f'Constraint {key}={schema[key]}'))
    if kind=='array':
        for index,item in enumerate(value): issues.extend(arguments_errors(item,schema.get('items',{}),(*path,index)))
    return issues

