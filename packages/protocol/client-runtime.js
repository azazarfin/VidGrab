// Shared runtime embedded by generate.py; no browser, Node or Tauri globals.
export class ProtocolError extends Error {
  constructor(code, message, requestId = null, details = {}) {
    super(message);
    this.name = 'ProtocolError';
    this.code = code;
    this.request_id = requestId;
    this.retryable = false;
    this.retry_at = null;
    this.provenance = 'local';
    this.details = details;
  }
}

function matches(schema, value, root = schema, depth = 0) {
  if (depth > 64) return false;
  if (schema.$ref) {
    const target = schema.$ref.slice(2).split('/').reduce((part, key) => part?.[key], root);
    return !!target && matches(target, value, root, depth + 1);
  }
  if (schema.anyOf && !schema.anyOf.some(part => matches(part, value, root, depth + 1))) return false;
  if (schema.oneOf && schema.oneOf.filter(part => matches(part, value, root, depth + 1)).length !== 1) return false;
  if (schema.enum && !schema.enum.some(item => Object.is(item, value))) return false;
  if (Object.hasOwn(schema, 'const') && !Object.is(schema.const, value)) return false;
  const types = {
    null: () => value === null,
    boolean: () => typeof value === 'boolean',
    number: () => typeof value === 'number' && Number.isFinite(value),
    integer: () => typeof value === 'number' && Number.isInteger(value),
    string: () => typeof value === 'string',
    array: () => Array.isArray(value),
    object: () => value !== null && typeof value === 'object' && !Array.isArray(value),
  };
  if (schema.type && !types[schema.type]?.()) return false;
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) return false;
    if (schema.maximum !== undefined && value > schema.maximum) return false;
  }
  if (typeof value === 'string') {
    const length = Array.from(value).length;
    if (schema.minLength !== undefined && length < schema.minLength) return false;
    if (schema.maxLength !== undefined && length > schema.maxLength) return false;
    if (schema.pattern && !new RegExp(schema.pattern, 'u').test(value)) return false;
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) return false;
    if (schema.maxItems !== undefined && value.length > schema.maxItems) return false;
    if (schema.items && !value.every(item => matches(schema.items, item, root, depth + 1))) return false;
  }
  if (types.object()) {
    if (schema.required?.some(key => !Object.hasOwn(value, key))) return false;
    for (const [key, item] of Object.entries(value)) {
      if (Object.hasOwn(schema.properties || {}, key)) {
        if (!matches(schema.properties[key], item, root, depth + 1)) return false;
      } else if (schema.additionalProperties === false) return false;
      else if (schema.additionalProperties && !matches(schema.additionalProperties, item, root, depth + 1)) return false;
    }
  }
  return true;
}

export function validateRequest(request, contract) {
  const operation = Object.hasOwn(contract.operations, request?.operation || '') ? contract.operations[request.operation] : null;
  return matches(contract.request, request) && !!operation && matches(operation.params, request.params)
    && (!operation.mutation || typeof request.idempotency_key === 'string')
    && new TextEncoder().encode(JSON.stringify(request)).length <= contract.max_payload_bytes;
}

export function makeClient(transport, contract) {
  return {
    async request(operation, params, options) {
      const request = { protocol_version: contract.protocol_version, request_id: options?.request_id, operation, params };
      if (options?.idempotency_key !== undefined) request.idempotency_key = options.idempotency_key;
      if (!validateRequest(request, contract)) {
        throw new ProtocolError('BAD_REQUEST', 'The request does not match the supported contract', options?.request_id);
      }
      let response;
      try {
        response = await transport(request);
      } catch (error) {
        if (error?.code && contract.response.$defs.Error.properties.code.enum.includes(error.code)) {
          throw new ProtocolError(error.code, error.message, request.request_id, error.details);
        }
        const mutation = contract.operations[operation].mutation;
        throw new ProtocolError(mutation ? 'REQUEST_OUTCOME_UNKNOWN' : 'ENGINE_UNAVAILABLE',
          mutation ? 'The request may have been accepted; reconnect with the same key to check it' : 'The engine is unavailable',
          request.request_id);
      }
      if (response?.protocol_version !== undefined && response.protocol_version !== contract.protocol_version) {
        throw new ProtocolError('APP_INCOMPATIBLE', 'Update VidGrab and the browser extension to compatible versions', request.request_id);
      }
      if (!matches(contract.response, response) || response.request_id !== request.request_id
          || (!response.ok && response.error.request_id !== request.request_id)
          || new TextEncoder().encode(JSON.stringify(response)).length > contract.max_payload_bytes) {
        throw new ProtocolError('BAD_RESPONSE', 'The companion returned an invalid response', request.request_id);
      }
      if (!response.ok) {
        const error = new ProtocolError(response.error.code, response.error.message, request.request_id, response.error.details);
        error.retryable = response.error.retryable;
        error.retry_at = response.error.retry_at;
        error.provenance = response.error.provenance;
        throw error;
      }
      if (!matches(contract.operations[operation].result, response.data)) {
        throw new ProtocolError('BAD_RESPONSE', 'The companion returned an invalid operation result', request.request_id);
      }
      return response.data;
    },
  };
}
