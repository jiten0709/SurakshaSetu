package com.surakshasetu.domain.common;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Type;
import java.nio.charset.StandardCharsets;
import org.springframework.core.MethodParameter;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpInputMessage;
import org.springframework.http.converter.HttpMessageConverter;
import org.springframework.web.bind.annotation.ControllerAdvice;
import org.springframework.web.context.request.RequestAttributes;
import org.springframework.web.context.request.RequestContextHolder;
import org.springframework.web.servlet.mvc.method.annotation.RequestBodyAdviceAdapter;

/**
 * Keeps each request body exactly as sent, before Jackson binds it. Decisions hash that JSON
 * ({@code inputs_sha256}, never a re-serialisation with defaults filled in) and read which optional
 * slots were sent at all.
 */
@ControllerAdvice
public class RawJson extends RequestBodyAdviceAdapter {

  private static final String ATTRIBUTE = RawJson.class.getName();

  @Override
  public boolean supports(
      MethodParameter parameter,
      Type targetType,
      Class<? extends HttpMessageConverter<?>> converterType) {
    return true;
  }

  @Override
  public HttpInputMessage beforeBodyRead(
      HttpInputMessage input,
      MethodParameter parameter,
      Type targetType,
      Class<? extends HttpMessageConverter<?>> converterType)
      throws IOException {
    byte[] body = input.getBody().readAllBytes();
    RequestContextHolder.currentRequestAttributes()
        .setAttribute(ATTRIBUTE, body, RequestAttributes.SCOPE_REQUEST);
    return new HttpInputMessage() {
      @Override
      public InputStream getBody() {
        return new ByteArrayInputStream(body);
      }

      @Override
      public HttpHeaders getHeaders() {
        return input.getHeaders();
      }
    };
  }

  /** The current request's body as sent. */
  public static String current() {
    byte[] body =
        (byte[])
            RequestContextHolder.currentRequestAttributes()
                .getAttribute(ATTRIBUTE, RequestAttributes.SCOPE_REQUEST);
    if (body == null) {
      throw new IllegalStateException("no request body was read");
    }
    return new String(body, StandardCharsets.UTF_8);
  }
}
