import { expect, test } from 'vitest'

test('similarly named file remains distinct', () => expect('alphabet').toContain('alpha'))
