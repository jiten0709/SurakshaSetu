package com.surakshasetu.domain.common;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpStatus;
import org.springframework.web.method.HandlerMethod;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * An operation whose controller still inherits the generated default method answers 501
 * problem+json. Overriding the method in the controller implements it.
 */
@Configuration
class NotImplementedStubs implements WebMvcConfigurer, HandlerInterceptor {

  @Override
  public void addInterceptors(InterceptorRegistry registry) {
    registry.addInterceptor(this);
  }

  @Override
  public boolean preHandle(
      HttpServletRequest request, HttpServletResponse response, Object handler) {
    if (handler instanceof HandlerMethod method && method.getMethod().isDefault()) {
      throw Problems.problem(
          HttpStatus.NOT_IMPLEMENTED,
          "NOT_IMPLEMENTED",
          method.getMethod().getName() + " is not implemented yet");
    }
    return true;
  }
}
