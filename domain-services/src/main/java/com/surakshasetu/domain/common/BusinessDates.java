package com.surakshasetu.domain.common;

import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import org.jspecify.annotations.Nullable;

/**
 * Instants are UTC; effective dates (product, disclosure, notice) are Indian calendar dates, so an
 * instant is compared with them on its IST date.
 */
public final class BusinessDates {

  public static final ZoneId ZONE = ZoneId.of("Asia/Kolkata");

  private BusinessDates() {}

  /** Now in UTC at the database's microsecond precision, so a stored value reads back equal. */
  public static OffsetDateTime now() {
    return OffsetDateTime.now(ZoneOffset.UTC).truncatedTo(ChronoUnit.MICROS);
  }

  public static OffsetDateTime orNow(@Nullable OffsetDateTime asOf) {
    return asOf != null ? asOf : now();
  }

  /** The IST calendar date of an instant; now when absent. */
  public static LocalDate on(@Nullable OffsetDateTime asOf) {
    return orNow(asOf).atZoneSameInstant(ZONE).toLocalDate();
  }
}
