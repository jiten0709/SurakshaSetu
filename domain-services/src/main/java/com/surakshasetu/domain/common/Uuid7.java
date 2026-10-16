package com.surakshasetu.domain.common;

import java.security.SecureRandom;
import java.util.UUID;

/** UUIDv7 (RFC 9562): 48-bit unix ms, version 7, 74 random bits. Every application id. */
public final class Uuid7 {

  private static final SecureRandom RANDOM = new SecureRandom();

  private Uuid7() {}

  public static UUID next() {
    long msb = (System.currentTimeMillis() << 16) | 0x7000L | (RANDOM.nextLong() & 0x0FFFL);
    long lsb = (RANDOM.nextLong() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
    return new UUID(msb, lsb);
  }
}
