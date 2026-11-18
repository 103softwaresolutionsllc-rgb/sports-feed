/**
 * contracts/validate.mjs — dependency-free validator for the apparatus
 * contract layer (Quant Apparatus spec §4, Phase A).
 *
 * validate(contractName, obj, { level }) → { ok, errors[] }
 *   level 'envelope' (default): the full contract form — envelope fields
 *     (contract, v, class, source, fetchedAt, id) plus payload fields,
 *     per the schema's `required`.
 *   level 'payload': only the contract's payload fields, per the
 *     schema's `payloadRequired` annotation — the level today's native
 *     payloads are held to after the documented field mapping
 *     (contracts/README.md) is applied.
 *
 * Supported JSON Schema subset (draft-07): type (string or array of
 * strings; integer/number/string/boolean/object/array/null), required,
 * properties, items (single schema), enum, const, additionalProperties
 * (false or a schema), minimum, maximum, minItems, minLength.
 * `format` is annotation-only and is NOT enforced.
 *
 * The validator never coerces: a string "0.5" is not a number, a missing
 * field is an error, an unexpected field is an error where the schema
 * sets additionalProperties: false. Failures come back as named errors
 * with $-paths, e.g. "$.legs[0].model.probability: expected type number,
 * got string". An unknown contract name throws — a misspelled seam must
 * be loud, never a silent pass.
 */

import { readFileSync } from 'node:fs';

const FILES = {
  game: 'game.schema.json',
  lineset: 'lineset.schema.json',
  pick: 'pick.schema.json',
  'parlay-snapshot': 'parlay-snapshot.schema.json',
  'fit-artifact': 'fit-artifact.schema.json',
  'grade-record': 'grade-record.schema.json',
  'source-health': 'source-health.schema.json',
};

const SCHEMAS = {};
for (const [name, file] of Object.entries(FILES)) {
  SCHEMAS[name] = JSON.parse(
    readFileSync(new URL(`./${file}`, import.meta.url), 'utf8')
  );
}

export function listContracts() {
  return Object.keys(SCHEMAS);
}

export function getSchema(contractName) {
  const schema = SCHEMAS[contractName];
  if (!schema) {
    throw new Error(
      `Unknown contract '${contractName}'. Known contracts: ${listContracts().join(', ')}`
    );
  }
  return schema;
}

function typeOf(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function matchesType(value, type) {
  switch (type) {
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value);
    case 'number':
      return typeof value === 'number' && Number.isFinite(value);
    case 'string':
      return typeof value === 'string';
    case 'boolean':
      return typeof value === 'boolean';
    case 'object':
      return value !== null && typeof value === 'object' && !Array.isArray(value);
    case 'array':
      return Array.isArray(value);
    case 'null':
      return value === null;
    default:
      return false;
  }
}

function validateNode(schema, value, path, errors, rootRequired) {
  if (!schema || typeof schema !== 'object') return;

  if (schema.const !== undefined && value !== schema.const) {
    errors.push(
      `${path}: expected constant ${JSON.stringify(schema.const)}, got ${JSON.stringify(value)}`
    );
    return;
  }

  if (schema.enum !== undefined) {
    const hit = schema.enum.some(
      (e) => e === value || JSON.stringify(e) === JSON.stringify(value)
    );
    if (!hit) {
      errors.push(
        `${path}: value ${JSON.stringify(value)} is not one of ${JSON.stringify(schema.enum)}`
      );
      return;
    }
  }

  if (schema.type !== undefined) {
    const types = Array.isArray(schema.type) ? schema.type : [schema.type];
    if (!types.some((t) => matchesType(value, t))) {
      errors.push(
        `${path}: expected type ${types.join(' | ')}, got ${typeOf(value)}`
      );
      return;
    }
  }

  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) {
      errors.push(`${path}: value ${value} is below minimum ${schema.minimum}`);
    }
    if (schema.maximum !== undefined && value > schema.maximum) {
      errors.push(`${path}: value ${value} is above maximum ${schema.maximum}`);
    }
  }

  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) {
      errors.push(
        `${path}: string is shorter than minLength ${schema.minLength}`
      );
    }
  }

  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) {
      errors.push(
        `${path}: array has ${value.length} items, fewer than minItems ${schema.minItems}`
      );
    }
    if (schema.items && typeof schema.items === 'object') {
      value.forEach((item, i) =>
        validateNode(schema.items, item, `${path}[${i}]`, errors, null)
      );
    }
  }

  if (matchesType(value, 'object')) {
    const required = rootRequired ?? schema.required ?? [];
    for (const key of required) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        errors.push(`${path}: missing required property '${key}'`);
      }
    }
    const props = schema.properties ?? {};
    for (const [key, propSchema] of Object.entries(props)) {
      if (Object.prototype.hasOwnProperty.call(value, key)) {
        validateNode(propSchema, value[key], `${path}.${key}`, errors, null);
      }
    }
    if (schema.additionalProperties === false) {
      for (const key of Object.keys(value)) {
        if (!Object.prototype.hasOwnProperty.call(props, key)) {
          errors.push(`${path}: unexpected property '${key}'`);
        }
      }
    } else if (
      schema.additionalProperties &&
      typeof schema.additionalProperties === 'object'
    ) {
      for (const key of Object.keys(value)) {
        if (!Object.prototype.hasOwnProperty.call(props, key)) {
          validateNode(
            schema.additionalProperties,
            value[key],
            `${path}.${key}`,
            errors,
            null
          );
        }
      }
    }
  }
}

export function validate(contractName, obj, { level = 'envelope' } = {}) {
  const schema = getSchema(contractName);
  if (level !== 'envelope' && level !== 'payload') {
    throw new Error(
      `Unknown validation level '${level}' — expected 'envelope' or 'payload'`
    );
  }
  const errors = [];
  if (!matchesType(obj, 'object')) {
    return {
      ok: false,
      errors: [`$: expected type object, got ${typeOf(obj)}`],
    };
  }
  const rootRequired =
    level === 'payload' ? (schema.payloadRequired ?? []) : null;
  validateNode(schema, obj, '$', errors, rootRequired);
  return { ok: errors.length === 0, errors };
}

export default validate;
