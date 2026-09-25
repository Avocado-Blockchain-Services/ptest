import { describe, expect, it } from 'vitest';
import { add } from './helpers';

describe('fill07', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });
});
