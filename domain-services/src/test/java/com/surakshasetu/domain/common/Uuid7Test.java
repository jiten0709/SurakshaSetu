package com.surakshasetu.domain.common;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class Uuid7Test {

  @Test
  void versionVariantAndTimestamp() {
    long before = System.currentTimeMillis();
    Set<UUID> ids = new HashSet<>();
    for (int i = 0; i < 10_000; i++) {
      UUID id = Uuid7.next();
      assertThat(id.version()).isEqualTo(7);
      assertThat(id.variant()).isEqualTo(2);
      ids.add(id);
    }
    long after = System.currentTimeMillis();
    long millis = Uuid7.next().getMostSignificantBits() >>> 16;

    assertThat(ids).hasSize(10_000);
    assertThat(millis).isBetween(before, after + 1_000);
  }
}
