import { describe, expect, test } from 'vitest'

describe('literal fixture names', () => {
  test('same title', () => expect(1).toBe(1))
  test('same title', () => expect(2).toBe(2))
})
