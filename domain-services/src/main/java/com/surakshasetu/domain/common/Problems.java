package com.surakshasetu.domain.common;

import java.math.BigDecimal;
import org.jspecify.annotations.Nullable;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.web.ErrorResponseException;

/**
 * Problem+json errors carrying the contract's machine-readable {@code code}. A detail never names a
 * customer, a consent id or a request value.
 */
public final class Problems {

  private Problems() {}

  public static ErrorResponseException problem(HttpStatus status, String code, String detail) {
    ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
    problem.setProperty("code", code);
    return new ErrorResponseException(status, problem, null);
  }

  public static ErrorResponseException notFound(String detail) {
    return problem(HttpStatus.NOT_FOUND, "NOT_FOUND", detail);
  }

  /**
   * 422 QUOTE_OUT_OF_BOUNDS: the request member {@code field} is outside the product's limits. The
   * bounds that apply go out as decimal strings; a null one is omitted.
   */
  public static ErrorResponseException outOfBounds(
      String field, @Nullable Object min, @Nullable Object max, @Nullable Object step) {
    ErrorResponseException e =
        problem(
            HttpStatus.UNPROCESSABLE_CONTENT,
            "QUOTE_OUT_OF_BOUNDS",
            field + " is outside the product's limits");
    ProblemDetail problem = e.getBody();
    problem.setProperty("field", field);
    Object[][] bounds = {{"allowed_min", min}, {"allowed_max", max}, {"allowed_step", step}};
    for (Object[] bound : bounds) {
      if (bound[1] != null) {
        problem.setProperty((String) bound[0], plain(bound[1]));
      }
    }
    return e;
  }

  private static String plain(Object value) {
    return value instanceof BigDecimal d
        ? d.stripTrailingZeros().toPlainString()
        : value.toString();
  }
}
