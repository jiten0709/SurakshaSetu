package com.surakshasetu.domain.common;

import jakarta.validation.ConstraintViolationException;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * Every error leaves as problem+json with a {@code code}. Framework errors (a malformed body, an
 * unknown route) carry the HTTP status name, e.g. {@code BAD_REQUEST}. Replaces Boot's default
 * problem-details handler.
 */
@RestControllerAdvice
class ProblemHandler extends ResponseEntityExceptionHandler {

  private static final Logger log = LoggerFactory.getLogger(ProblemHandler.class);

  @Override
  protected ResponseEntity<Object> createResponseEntity(
      @Nullable Object body, HttpHeaders headers, HttpStatusCode status, WebRequest request) {
    if (body instanceof ProblemDetail problem
        && (problem.getProperties() == null || !problem.getProperties().containsKey("code"))) {
      problem.setProperty("code", codeFor(status));
    }
    return super.createResponseEntity(body, headers, status, request);
  }

  /**
   * A path or query value breaking a contract constraint (a UIN or pincode pattern). The generated
   * interfaces are @Validated, so it arrives as a ConstraintViolationException.
   */
  @ExceptionHandler(ConstraintViolationException.class)
  ResponseEntity<Object> constraintViolation(ConstraintViolationException ex, WebRequest request) {
    ProblemDetail problem =
        ProblemDetail.forStatusAndDetail(
            HttpStatus.BAD_REQUEST, "a request value breaks a constraint of the contract");
    return createResponseEntity(problem, new HttpHeaders(), HttpStatus.BAD_REQUEST, request);
  }

  /**
   * Anything unexpected is a 500. Only the class and the throwing frame are logged: a driver
   * message can quote row values (a PSQLException names the conflicting key).
   */
  @ExceptionHandler(Exception.class)
  ResponseEntity<Object> unexpected(Exception ex, WebRequest request) {
    StackTraceElement[] trace = ex.getStackTrace();
    log.error(
        "unhandled {} at {}", ex.getClass().getName(), trace.length > 0 ? trace[0] : "unknown");
    ProblemDetail problem = ProblemDetail.forStatus(HttpStatus.INTERNAL_SERVER_ERROR);
    return createResponseEntity(
        problem, new HttpHeaders(), HttpStatus.INTERNAL_SERVER_ERROR, request);
  }

  private static String codeFor(HttpStatusCode status) {
    HttpStatus known = HttpStatus.resolve(status.value());
    return known != null ? known.name() : "HTTP_" + status.value();
  }
}
