package com.example.blog.service;

import com.example.blog.dto.UserDto;

public class UserPrinter {
    public String describe(UserDto user) {
        return user.getEmail() + " / " + user.getName();
    }

    public UserDto rename(UserDto user, String newName) {
        user.setName(newName);
        return user;
    }
}
