package com.surakshasetu.domain.common;

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
}
